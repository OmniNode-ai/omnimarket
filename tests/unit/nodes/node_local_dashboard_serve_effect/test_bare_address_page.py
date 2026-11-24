# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 (OMN-26226): a browser gets a readable page; API reads still need the token."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.projection.test_local_serving_auth import _PORT, _started

pytestmark = pytest.mark.unit


def test_a_browser_gets_a_readable_html_page(tmp_path) -> None:
    app, _token = _started(tmp_path)
    client = TestClient(app, base_url=f"http://127.0.0.1:{_PORT}")
    response = client.get("/", headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    assert "onex dashboard open" in response.text
    assert "enter" in response.text.lower() or "Open" in response.text


def test_a_json_read_without_the_token_still_gets_401(tmp_path) -> None:
    app, _token = _started(tmp_path)
    client = TestClient(app, base_url=f"http://127.0.0.1:{_PORT}")
    response = client.get("/")
    assert response.status_code == 401
    assert response.json()["error"] == "token_required"


def test_an_api_read_without_the_token_still_gets_401(tmp_path) -> None:
    app, _token = _started(tmp_path)
    client = TestClient(app, base_url=f"http://127.0.0.1:{_PORT}")
    response = client.get("/projections")
    assert response.status_code == 401
    assert response.json()["error"] == "token_required"
