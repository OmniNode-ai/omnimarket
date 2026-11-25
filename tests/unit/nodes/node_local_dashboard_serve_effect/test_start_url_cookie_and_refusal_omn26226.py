# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC5 (OMN-26226): the start URL's cookie is HttpOnly, and a wrong or missing token is refused.

AC5 asks that opening the start address with this start's token sets the HttpOnly cookie,
and that a read with a wrong or missing token is still refused. These tests check those two
clauses on the start URL itself and on a read, so a cookie that loses HttpOnly, or a token
check that accepts any value, fails here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnimarket.nodes.node_local_dashboard_serve_effect.handlers.handler_local_dashboard_serve import (
    DASHBOARD_TOKEN_PARAM,
    dashboard_cookie_name,
)
from tests.projection.test_local_serving_auth import _PORT, _READ, _started

pytestmark = pytest.mark.unit

_HTML = {"Accept": "text/html"}
_COOKIE = dashboard_cookie_name(_PORT)


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, base_url=f"http://127.0.0.1:{_PORT}")


def test_the_start_url_sets_an_httponly_cookie(tmp_path: Path) -> None:
    app, token = _started(tmp_path)
    response = _client(app).get(
        "/",
        params={DASHBOARD_TOKEN_PARAM: token},
        headers=_HTML,
        follow_redirects=False,
    )
    assert response.status_code == 303
    set_cookie = response.headers.get("set-cookie", "")
    assert set_cookie.startswith(f"{_COOKIE}=")
    assert "httponly" in set_cookie.lower()
    assert "samesite=strict" in set_cookie.lower()


def test_a_wrong_token_on_the_start_url_gets_no_cookie_and_no_redirect(
    tmp_path: Path,
) -> None:
    app, token = _started(tmp_path)
    response = _client(app).get(
        "/",
        params={DASHBOARD_TOKEN_PARAM: "x" * len(token)},
        headers=_HTML,
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    assert "needs this start's token" in response.text


@pytest.mark.parametrize("channel", ["query", "bearer", "cookie"])
def test_a_read_with_a_wrong_token_is_refused(tmp_path: Path, channel: str) -> None:
    app, token = _started(tmp_path)
    client = _client(app)
    wrong = "x" * len(token)
    params: dict[str, str] = {}
    headers: dict[str, str] = {}
    if channel == "query":
        params = {DASHBOARD_TOKEN_PARAM: wrong}
    elif channel == "bearer":
        headers = {"Authorization": f"Bearer {wrong}"}
    else:
        client.cookies.set(_COOKIE, wrong)
    response = client.get(_READ, params=params, headers=headers)
    assert response.status_code == 401
    assert response.json()["error"] == "token_required"
    assert "set-cookie" not in response.headers


def test_a_read_with_no_token_is_refused(tmp_path: Path) -> None:
    app, _token = _started(tmp_path)
    response = _client(app).get(_READ)
    assert response.status_code == 401
    assert response.json()["error"] == "token_required"
