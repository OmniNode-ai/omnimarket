# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local dashboard answers only on loopback and only to this start's token (OMN-19916).

Decision D2 (RULING 2026-10-02T01:10:19Z): ``onex dashboard`` binds loopback
only AND requires a token minted at each start. A loopback bind alone lets any
local process, or a DNS-rebinding page in the developer's own browser, read
their prompts and answers.

Every case here drives the app the way ``onex dashboard`` builds it: through
:class:`HandlerLocalDashboardServe` with a capturing ``serve``, so the guard
under test is the one production wires, not a second copy of it. Case 1 is the
positive control: four refusals from a process that serves nothing prove
nothing.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnimarket.cli import cli_local
from omnimarket.nodes.node_local_dashboard_serve_effect.handlers.handler_local_dashboard_serve import (
    DASHBOARD_TOKEN_PARAM,
    HandlerLocalDashboardServe,
    dashboard_cookie_name,
)
from omnimarket.nodes.node_local_dashboard_serve_effect.models import (
    DashboardBindError,
    ModelLocalDashboardServeRequest,
    resolve_dashboard_bind,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.discovery import parse_order_by_clauses
from omnimarket.projection.models import ProjectionTableConfig

pytestmark = pytest.mark.unit

_TENANT = "19916000-0000-4000-8000-000000000001"
_TOPIC = "onex.snapshot.projection.delegation.decisions.v1"
_PORT = 7616
_LOOPBACK = f"http://127.0.0.1:{_PORT}"
_READ = f"/projection/{_TOPIC}"


def _topic_map() -> dict[str, ProjectionTableConfig]:
    columns = ("correlation_id", "tenant_id", "written_at")
    cfg = ProjectionTableConfig(
        topic=_TOPIC,
        table="delegation_events",
        schema_name="public",
        relation_schema="public",
        columns=columns,
        order_by="written_at DESC",
        order_by_spec=parse_order_by_clauses("written_at DESC", columns),
        freshness_column="written_at",
        cursor_column="written_at",
        limit=500,
        bus_backed=True,
        key_columns=("correlation_id",),
        tenant_column="tenant_id",
    )
    return {cfg.topic: cfg}


def _store(tmp_path: Path) -> Path:
    db_path = tmp_path / "delegation.sqlite"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE delegation_events "
            "(correlation_id TEXT, tenant_id TEXT, written_at TEXT)"
        )
        conn.execute(
            "INSERT INTO delegation_events VALUES (?, ?, ?)",
            ("19916-row", _TENANT, "2026-10-15T12:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _started(tmp_path: Path) -> tuple[FastAPI, str]:
    """One start, as ``onex dashboard`` makes it: the app it serves and its token."""
    pages = tmp_path / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    (pages / "index.html").write_text("<html>dashboard</html>", encoding="utf-8")
    served: list[FastAPI] = []

    async def serve(app: FastAPI, _host: str, _port: int) -> None:
        served.append(app)

    request = ModelLocalDashboardServeRequest(
        host="127.0.0.1", port=_PORT, tenant_id=_TENANT
    )
    asyncio.run(
        HandlerLocalDashboardServe(
            topic_map=_topic_map(),
            row_source=SqliteTableRowSource(_store(tmp_path)),
            serve=serve,
            pages=pages,
        ).handle(request)
    )
    [app] = served
    return app, request.access_token.get_secret_value()


def _client(app: FastAPI, base_url: str = _LOOPBACK) -> TestClient:
    return TestClient(app, base_url=base_url)


# -- case 1, the positive control: the current token is served ------------------


@pytest.mark.parametrize("path", ["/projections", _READ, "/"])
def test_the_current_token_in_the_query_is_served(tmp_path: Path, path: str) -> None:
    app, token = _started(tmp_path)
    response = _client(app).get(path, params={DASHBOARD_TOKEN_PARAM: token})
    assert response.status_code == 200, response.text


def test_the_current_token_as_a_bearer_header_is_served(tmp_path: Path) -> None:
    app, token = _started(tmp_path)
    response = _client(app).get(_READ, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    assert [row["correlation_id"] for row in response.json()["rows"]] == ["19916-row"]


def test_opening_the_start_url_trades_the_token_for_an_httponly_cookie(
    tmp_path: Path,
) -> None:
    app, token = _started(tmp_path)
    client = _client(app)
    opened = client.get("/", params={DASHBOARD_TOKEN_PARAM: token})
    assert opened.status_code == 200
    set_cookie = opened.headers["set-cookie"]
    assert set_cookie.startswith(f"{dashboard_cookie_name(_PORT)}=")
    assert "httponly" in set_cookie.lower()
    assert "samesite=strict" in set_cookie.lower()
    # The page's own reads then carry only the cookie, as a browser sends them.
    assert client.get(_READ).status_code == 200


# -- case 2: a stale token, minted by an earlier start, is refused ---------------


def test_each_start_mints_a_different_token() -> None:
    first = ModelLocalDashboardServeRequest(host="127.0.0.1", port=_PORT)
    second = ModelLocalDashboardServeRequest(host="127.0.0.1", port=_PORT)
    assert (
        first.access_token.get_secret_value() != second.access_token.get_secret_value()
    )
    assert len(first.access_token.get_secret_value()) >= 32
    assert first.access_token.get_secret_value() not in repr(first)


def test_an_earlier_starts_token_is_refused_on_every_channel(tmp_path: Path) -> None:
    _old_app, stale = _started(tmp_path / "first")
    app, _token = _started(tmp_path / "second")
    client = _client(app)
    client.cookies.set(dashboard_cookie_name(_PORT), stale)
    response = client.get(
        _READ,
        params={DASHBOARD_TOKEN_PARAM: stale},
        headers={"Authorization": f"Bearer {stale}"},
    )
    assert response.status_code == 401
    assert "set-cookie" not in response.headers


# -- case 3: no token is refused, the page included ------------------------------


@pytest.mark.parametrize("path", ["/projections", _READ, "/", "/delegations"])
def test_no_token_is_refused(tmp_path: Path, path: str) -> None:
    app, _token = _started(tmp_path)
    response = _client(app).get(path)
    assert response.status_code == 401
    assert response.json()["error"] == "token_required"


# -- case 4: a non-loopback interface is refused at start ------------------------


@pytest.mark.parametrize("bind", ["0.0.0.0:7616", "203.0.113.7:7616"])
def test_a_non_loopback_bind_is_refused_at_start(bind: str) -> None:
    with pytest.raises(DashboardBindError):
        resolve_dashboard_bind({"dashboard": {"bind": bind}})


# -- case 5: cross-origin and DNS-rebinding reads are refused --------------------


def test_a_dns_rebinding_host_is_refused_even_with_the_token(tmp_path: Path) -> None:
    app, token = _started(tmp_path)
    client = _client(app, base_url=f"http://c32-rebind.invalid:{_PORT}")
    for headers in ({}, {"Authorization": f"Bearer {token}"}):
        response = client.get(_READ, headers=headers)
        assert response.status_code == 403
        assert response.json()["error"] == "host_not_allowed"


def test_a_cross_origin_read_with_the_token_is_refused(tmp_path: Path) -> None:
    app, token = _started(tmp_path)
    response = _client(app).get(
        _READ,
        headers={
            "Authorization": f"Bearer {token}",
            "Origin": "http://c32-cross-origin.invalid",
        },
    )
    assert response.status_code == 403
    assert response.json()["error"] == "origin_not_allowed"
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize(
    "host", [f"127.0.0.1:{_PORT}", f"localhost:{_PORT}", f"[::1]:{_PORT}"]
)
def test_the_pages_own_origin_is_served(tmp_path: Path, host: str) -> None:
    app, token = _started(tmp_path)
    response = _client(app).get(
        _READ,
        headers={
            "Host": host,
            "Origin": f"http://{host}",
            "Authorization": f"Bearer {token}",
        },
    )
    assert response.status_code == 200, response.text


# -- the start line hands the developer the token, once --------------------------


def test_onex_dashboard_prints_the_start_url_with_this_starts_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    handed: list[ModelLocalDashboardServeRequest] = []

    class _Recorder:
        async def handle(self, request: ModelLocalDashboardServeRequest) -> None:
            handed.append(request)

    monkeypatch.setattr(cli_local, "HandlerLocalDashboardServe", _Recorder)
    monkeypatch.setattr(cli_local, "read_local_tenant_identity", lambda: None)
    overlay = tmp_path / "bind.yaml"
    overlay.write_text(f'dashboard:\n  bind: "127.0.0.1:{_PORT}"\n', encoding="utf-8")
    result = CliRunner().invoke(
        cli_local.dashboard_command, ["--overlay", str(overlay)]
    )
    assert result.exit_code == 0, result.output
    [request] = handed
    token = request.access_token.get_secret_value()
    assert f"http://127.0.0.1:{_PORT}/?{DASHBOARD_TOKEN_PARAM}={token}" in result.output
    assert result.output.count(token) == 1
