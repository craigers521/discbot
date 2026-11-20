"""Unit tests for the Flask app factory (T023) and dashboard serving
(T042, contracts/management-api.md "Dashboard contract", FR-019).

The single app serves both the ``/api/*`` blueprint and the static
dashboard: ``GET /`` returns the index shell and ``/static/*`` serves
``botalert/static`` — the shell is public (it holds no data), while
every ``/api/*`` route except health still requires the token.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from flask import Flask

from botalert.models import RuntimeFlags
from botalert.state import StateStore
from botalert.web import create_app
from tests.fakes import FakeScheduler
from tests.helpers import make_config

TOKEN = "test-shared-secret"


@pytest.fixture()
def parts(tmp_path: Path) -> tuple[Flask, StateStore, FakeScheduler, RuntimeFlags]:
    config = make_config()
    state = StateStore(str(tmp_path / "state.json"), config)
    scheduler = FakeScheduler()
    flags = RuntimeFlags()
    app = create_app(config, state, scheduler, flags)
    return app, state, scheduler, flags


@pytest.fixture()
def client(parts: tuple[Flask, StateStore, FakeScheduler, RuntimeFlags]) -> Any:
    return parts[0].test_client()


class TestDashboardServing:
    """T042: GET / serves the shell; /static/* serves the package assets."""

    def test_root_serves_index_html(self, client: Any) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert response.content_type.startswith("text/html")
        html: str = response.get_data(as_text=True)
        # It is the dashboard shell, not a directory listing or an error page.
        assert '<script src="/static/app.js"></script>' in html
        assert 'id="token-view"' in html
        assert 'id="products-body"' in html

    def test_root_is_public_no_token_needed(self, client: Any) -> None:
        # The shell itself is token-free (research.md §8): only /api/* is gated.
        assert client.get("/").status_code == 200

    def test_static_app_js_served_with_js_mime(self, client: Any) -> None:
        response = client.get("/static/app.js")
        assert response.status_code == 200
        assert response.content_type.startswith("text/javascript")
        body: str = response.get_data(as_text=True)
        assert "botalert-token" in body  # the real dashboard script

    def test_static_missing_file_404(self, client: Any) -> None:
        assert client.get("/static/nope.js").status_code == 404

    def test_api_blueprint_registered_on_same_app(self, client: Any) -> None:
        # One app, one port (FR-013): the dashboard and the API coexist.
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/products").status_code == 401
        assert (
            client.get(
                "/api/products", headers={"X-Botalert-Token": TOKEN}
            ).status_code
            == 200
        )
