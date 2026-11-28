# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC5 (OMN-26226): opening the start URL with the token redirects without it."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.projection.test_local_serving_auth import (
    _PORT,
    DASHBOARD_TOKEN_PARAM,
    _started,
)

pytestmark = pytest.mark.unit


def test_opening_the_start_url_sets_a_cookie_and_redirects_without_the_token(
    tmp_path,
) -> None:
    app, token = _started(tmp_path)
    client = TestClient(app, base_url=f"http://127.0.0.1:{_PORT}")
    response = client.get(
        "/",
        params={DASHBOARD_TOKEN_PARAM: token},
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert DASHBOARD_TOKEN_PARAM not in location
    assert token not in location
    cookie_name = f"onex_dashboard_{_PORT}"
    assert cookie_name in response.headers.get("set-cookie", "")
    # A read with the cookie works.
    followup = client.get("/")
    assert followup.status_code == 200


def test_a_redirect_keeps_the_other_query_parameters(tmp_path) -> None:
    app, token = _started(tmp_path)
    client = TestClient(app, base_url=f"http://127.0.0.1:{_PORT}")
    correlation_id = "run-123"
    response = client.get(
        "/",
        params={DASHBOARD_TOKEN_PARAM: token, "correlation_id": correlation_id},
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert "correlation_id=run-123" in location
    assert DASHBOARD_TOKEN_PARAM not in location


def test_a_missing_token_still_gets_the_readable_page(tmp_path) -> None:
    app, _token = _started(tmp_path)
    client = TestClient(app, base_url=f"http://127.0.0.1:{_PORT}")
    response = client.get("/", headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")


def test_a_json_read_with_the_token_still_returns_200(tmp_path) -> None:
    app, token = _started(tmp_path)
    client = TestClient(app, base_url=f"http://127.0.0.1:{_PORT}")
    response = client.get("/", params={DASHBOARD_TOKEN_PARAM: token})
    assert response.status_code == 200
