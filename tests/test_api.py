"""Unit tests for the US1 management API (task T016, contracts/management-api.md).

Auth boundary (SC-008): GET /api/health public; every other /api/* route
401s on missing/wrong X-Botalert-Token before any state access. Plus
health, product list/add, and check-now with the contract error shape.
(PATCH/DELETE + config land in US3/US4 tasks.)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from flask import Flask

from botalert.api import create_api_blueprint
from botalert.models import RuntimeFlags
from botalert.state import StateStore
from tests.fakes import FakeScheduler
from tests.helpers import make_config

TOKEN = "test-shared-secret"
HEX12 = re.compile(r"^[0-9a-f]{12}$")


@pytest.fixture()
def parts(tmp_path: Path) -> tuple[Flask, StateStore, FakeScheduler, RuntimeFlags]:
    config = make_config()
    state = StateStore(str(tmp_path / "state.json"), config)
    scheduler = FakeScheduler()
    flags = RuntimeFlags()
    app = Flask(__name__)
    app.register_blueprint(create_api_blueprint(config, state, scheduler, flags))
    return app, state, scheduler, flags


@pytest.fixture()
def client(parts: tuple[Flask, StateStore, FakeScheduler, RuntimeFlags]) -> Any:
    return parts[0].test_client()


def headers(token: str | None = None) -> dict[str, str]:
    return {"X-Botalert-Token": token} if token is not None else {}


def assert_error(response: Any, code: str, status: int) -> dict[str, Any]:
    assert response.status_code == status
    body: dict[str, Any] = response.get_json()
    assert set(body) == {"error"}
    error: dict[str, Any] = body["error"]
    assert error["code"] == code
    message = error["message"]
    assert isinstance(message, str) and message
    return error


class TestHealth:
    def test_health_is_public_and_shape(self, client: Any) -> None:
        response = client.get("/api/health")
        assert response.status_code == 200
        body = response.get_json()
        assert body == {
            "status": "ok",
            "gateway_connected": False,
            "cycle_in_progress": False,
        }

    def test_health_reflects_runtime_flags(self, parts: tuple[Flask, StateStore, FakeScheduler, RuntimeFlags], client: Any) -> None:
        flags = parts[3]
        flags.gateway_connected = True
        flags.cycle_in_progress = True
        body = client.get("/api/health").get_json()
        assert body["gateway_connected"] is True
        assert body["cycle_in_progress"] is True


class TestAuthBoundary:
    def test_missing_token_401_on_every_protected_endpoint(self, client: Any) -> None:
        for method, url, body in [
            ("get", "/api/products", None),
            ("post", "/api/products", {"url": "https://x.example.com/a"}),
            ("post", "/api/check-now", None),
            ("get", "/api/config", None),
            ("put", "/api/config", {"cadence_minutes": 30}),
        ]:
            response = getattr(client, method)(url, json=body)
            error = assert_error(response, "unauthorized", 401)
            assert "token" in error["message"].lower() or error["message"]

    def test_wrong_token_401(self, client: Any) -> None:
        response = client.get("/api/products", headers=headers("wrong-token"))
        assert_error(response, "unauthorized", 401)

    def test_rejected_requests_alter_no_state(self, parts: tuple[Flask, StateStore, FakeScheduler, RuntimeFlags], client: Any) -> None:
        state = parts[1]
        state.add_product("https://seed.example.com/a")  # 1 product beforehand
        # Unauthenticated reads and writes must both fail and touch nothing.
        assert client.get("/api/products").status_code == 401
        response = client.post(
            "/api/products", json={"url": "https://x.example.com/new"}
        )
        assert response.status_code == 401
        assert len(state.get_products()) == 1  # unchanged


class TestListProducts:
    def test_empty_list(self, client: Any) -> None:
        response = client.get("/api/products", headers=headers(TOKEN))
        assert response.status_code == 200
        assert response.get_json() == {"products": []}

    def test_insertion_order_and_contract_shape(self, parts: tuple[Flask, StateStore, FakeScheduler, RuntimeFlags], client: Any) -> None:
        state = parts[1]
        first = state.add_product("https://a.example.com/one", label="One")
        second = state.add_product("https://b.example.com/two", label="Two")
        body = client.get("/api/products", headers=headers(TOKEN)).get_json()
        products = body["products"]
        assert [p["id"] for p in products] == [first.id, second.id]
        for product in products:
            assert set(product) == {
                "id",
                "url",
                "label",
                "in_stock_indicator",
                "out_of_stock_indicator",
                "last_status",
                "last_checked_at",
                "consecutive_failures",
                "created_at",
            }
            assert "failure_notified" not in product  # never exposed


class TestAddProduct:
    def test_minimal_add_returns_201_created(self, parts: tuple[Flask, StateStore, FakeScheduler, RuntimeFlags], client: Any) -> None:
        state = parts[1]
        scheduler = parts[2]
        response = client.post(
            "/api/products",
            json={"url": "https://x.example.com/a"},
            headers=headers(TOKEN),
        )
        assert response.status_code == 201
        body = response.get_json()
        assert HEX12.fullmatch(body["id"])
        assert body["url"] == "https://x.example.com/a"
        assert body["label"] == "x.example.com"  # defaults to host
        assert body["last_status"] == "unknown"
        assert body["last_checked_at"] is None
        assert body["consecutive_failures"] == 0
        assert len(state.get_products()) == 1
        assert scheduler.wakeups[-1] is True  # fresh list checked soon (SC-001)

    def test_full_add_with_label_and_indicators(self, client: Any) -> None:
        response = client.post(
            "/api/products",
            json={
                "url": "  HTTPS://X.example.com/A  ",
                "label": "Sneakers",
                "in_stock_indicator": "Add to Cart",
                "out_of_stock_indicator": "Out of Stock",
            },
            headers=headers(TOKEN),
        )
        assert response.status_code == 201
        body = response.get_json()
        assert body["url"] == "https://x.example.com/A"  # normalized
        assert body["label"] == "Sneakers"
        assert body["in_stock_indicator"] == "Add to Cart"
        assert body["out_of_stock_indicator"] == "Out of Stock"

    def test_invalid_url_codes(self, client: Any) -> None:
        for payload in [{"url": "not a url"}, {}, {"url": 42}, {"url": "ftp://x/a"}]:
            response = client.post("/api/products", json=payload, headers=headers(TOKEN))
            assert_error(response, "invalid_url", 400)

    def test_invalid_label(self, client: Any) -> None:
        for label in ["", "   ", "x" * 101]:
            response = client.post(
                "/api/products",
                json={"url": "https://x.example.com/a", "label": label},
                headers=headers(TOKEN),
            )
            assert_error(response, "invalid_label", 400)

    def test_invalid_indicator(self, client: Any) -> None:
        response = client.post(
            "/api/products",
            json={"url": "https://x.example.com/a", "in_stock_indicator": "i" * 201},
            headers=headers(TOKEN),
        )
        assert_error(response, "invalid_indicator", 400)

    def test_duplicate_url_409_with_contract_message(self, parts: tuple[Flask, StateStore, FakeScheduler, RuntimeFlags], client: Any) -> None:
        state = parts[1]
        state.add_product("https://shop.example.com/Item")
        response = client.post(
            "/api/products",
            json={"url": "HTTPS://SHOP.example.com/Item"},
            headers=headers(TOKEN),
        )
        error = assert_error(response, "duplicate_url", 409)
        assert error["message"] == "A product with this URL is already monitored."
        assert len(state.get_products()) == 1  # no silent merge, no second row


class TestCheckNow:
    def test_cycle_scheduled_202_and_wakeup(self, parts: tuple[Flask, StateStore, FakeScheduler, RuntimeFlags], client: Any) -> None:
        scheduler = parts[2]
        response = client.post("/api/check-now", headers=headers(TOKEN))
        assert response.status_code == 202
        assert response.get_json() == {"status": "cycle_scheduled"}
        assert scheduler.wakeups[-1] is True

    def test_409_when_cycle_in_progress(self, parts: tuple[Flask, StateStore, FakeScheduler, RuntimeFlags], client: Any) -> None:
        scheduler = parts[2]
        flags = parts[3]
        flags.cycle_in_progress = True
        before = len(scheduler.wakeups)
        response = client.post("/api/check-now", headers=headers(TOKEN))
        assert_error(response, "cycle_in_progress", 409)
        assert len(scheduler.wakeups) == before  # no second cycle started


class TestConfig:
    """GET/PUT /api/config — the US3 cadence control (SC-004, FR-005/011)."""

    def test_get_default_shape(self, client: Any) -> None:
        response = client.get("/api/config", headers=headers(TOKEN))
        assert response.status_code == 200
        assert response.get_json() == {
            "cadence_minutes": 5.0,
            "min_cadence_minutes": 1.0,
            "max_cadence_minutes": 1440.0,
        }

    def test_put_updates_state_echoes_and_wakes(
        self, parts: tuple[Flask, StateStore, FakeScheduler, RuntimeFlags], client: Any
    ) -> None:
        state = parts[1]
        scheduler = parts[2]
        response = client.put(
            "/api/config", json={"cadence_minutes": 30}, headers=headers(TOKEN)
        )
        assert response.status_code == 200
        assert response.get_json() == {
            "cadence_minutes": 30.0,
            "min_cadence_minutes": 1.0,
            "max_cadence_minutes": 1440.0,
        }
        assert state.get_cadence() == 30.0
        assert scheduler.wakeups[-1] is True  # takes effect from the next cycle

    def test_put_float_cadence(
        self, parts: tuple[Flask, StateStore, FakeScheduler, RuntimeFlags], client: Any
    ) -> None:
        state = parts[1]
        response = client.put(
            "/api/config", json={"cadence_minutes": 2.5}, headers=headers(TOKEN)
        )
        assert response.status_code == 200
        assert response.get_json()["cadence_minutes"] == 2.5
        assert state.get_cadence() == 2.5

    def test_put_boundaries_inclusive(
        self, parts: tuple[Flask, StateStore, FakeScheduler, RuntimeFlags], client: Any
    ) -> None:
        state = parts[1]
        for value in (1.0, 1440.0):
            response = client.put(
                "/api/config", json={"cadence_minutes": value}, headers=headers(TOKEN)
            )
            assert response.status_code == 200, f"{value} must be accepted"
        assert state.get_cadence() == 1440.0

    def test_put_invalid_rejected_exact_message_state_unchanged_no_wakeup(
        self, parts: tuple[Flask, StateStore, FakeScheduler, RuntimeFlags], client: Any
    ) -> None:
        state = parts[1]
        scheduler = parts[2]
        for value in ("abc", 0.5, 2000, True, "5.5", None):
            response = client.put(
                "/api/config", json={"cadence_minutes": value}, headers=headers(TOKEN)
            )
            error = assert_error(response, "invalid_cadence", 400)
            assert error["message"] == (
                "Cadence must be at least 1.0 minutes (minimum) "
                "and at most 1440.0 minutes (maximum)."
            )
        assert state.get_cadence() == 5.0  # unchanged in every case
        assert scheduler.wakeups == [False]  # no wakeup was issued

    def test_put_missing_key_rejected(
        self, parts: tuple[Flask, StateStore, FakeScheduler, RuntimeFlags], client: Any
    ) -> None:
        state = parts[1]
        for body in ({}, {"other": 3}, [30]):
            response = client.put("/api/config", json=body, headers=headers(TOKEN))
            assert_error(response, "invalid_cadence", 400)
        assert state.get_cadence() == 5.0

    def test_get_reflects_put(
        self, parts: tuple[Flask, StateStore, FakeScheduler, RuntimeFlags], client: Any
    ) -> None:
        client.put("/api/config", json={"cadence_minutes": 30}, headers=headers(TOKEN))
        body = client.get("/api/config", headers=headers(TOKEN)).get_json()
        assert body["cadence_minutes"] == 30.0
