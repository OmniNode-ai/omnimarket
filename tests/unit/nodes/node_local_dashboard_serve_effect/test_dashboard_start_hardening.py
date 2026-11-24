# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26226: the start token is per start, and the start redirect stays on this origin."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omnimarket.nodes.node_local_dashboard_serve_effect.models.model_local_dashboard import (
    ModelLocalDashboardServeRequest,
)
from tests.projection.test_local_serving_auth import (
    _PORT,
    DASHBOARD_TOKEN_PARAM,
    _started,
)

pytestmark = pytest.mark.unit

_TOKEN_FILE_ENV = "ONEX_DASHBOARD_TOKEN_FILE"


def _token_of(request: ModelLocalDashboardServeRequest) -> str:
    return request.access_token.get_secret_value()


def test_the_token_file_the_start_names_is_read_once_wherever_it_is(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    (home / ".onex").mkdir(parents=True)
    named = tmp_path / "elsewhere" / "start.token"
    named.parent.mkdir()
    named.write_text("handed-over-token\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(_TOKEN_FILE_ENV, str(named))

    first = ModelLocalDashboardServeRequest(host="127.0.0.1", port=12345)

    assert _token_of(first) == "handed-over-token"
    assert not named.exists()
    second = ModelLocalDashboardServeRequest(host="127.0.0.1", port=12345)
    assert _token_of(second) != "handed-over-token"


def test_a_token_file_left_behind_by_an_earlier_start_is_never_reused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    (home / ".onex").mkdir(parents=True)
    (home / ".onex" / "dashboard.token").write_text("stale-token", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(_TOKEN_FILE_ENV, raising=False)

    request = ModelLocalDashboardServeRequest(host="127.0.0.1", port=12345)

    assert _token_of(request) != "stale-token"


def test_a_double_slash_path_does_not_redirect_off_this_origin(tmp_path: Path) -> None:
    app, token = _started(tmp_path)
    client = TestClient(app, base_url=f"http://127.0.0.1:{_PORT}")

    response = client.get(
        f"http://127.0.0.1:{_PORT}//evil.example/page",
        params={DASHBOARD_TOKEN_PARAM: token},
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/")
    assert not location.startswith("//")
