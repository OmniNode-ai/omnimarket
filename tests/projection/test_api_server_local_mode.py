# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex dashboard``'s local serving mode (OMN-19976 AC1, plan T2.3).

The local dashboard reads the developer's own SQLite store through the
runtime-resident projection read node (decision D1 (a), OMN-20159), not a second
query service. Each test names the failure it exists to catch:

* a topic no contract declares is served (it must answer 404);
* a declared exposure whose table holds no rows answers 404/503 or a fabricated
  timestamp (it must answer 200 with an empty list and ``as_of: null``);
* a request naming another tenant is served that tenant's rows (it must answer
  422), and a read naming no tenant is served unscoped;
* SQL text leaks out of the store adapter into the serving shim;
* the shim bypasses the read node, binds port 3002 or a non-loopback interface,
  or ignores the ``dashboard.bind`` overlay key;
* with no projection binding configured, the shim reads anything but the store
  the local writers fill;
* a store the real delegation writer created cannot serve the Runs exposure
  (it must answer 200 with the written row and every declared column).
"""

from __future__ import annotations

import ast
import asyncio
import re
import sqlite3
import tomllib
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from omnimarket.nodes.node_local_dashboard_serve_effect.handlers import (
    handler_local_dashboard_serve as cli_dashboard,
)
from omnimarket.nodes.node_local_dashboard_serve_effect.handlers.handler_local_dashboard_serve import (
    HandlerLocalDashboardServe,
    ProtocolProjectionReadNode,
    create_dashboard_app,
    resolve_local_row_source,
)
from omnimarket.nodes.node_local_dashboard_serve_effect.models import (
    DashboardBindError,
    ModelLocalDashboardServeRequest,
    resolve_dashboard_bind,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.models import (
    ModelProjectionReadRequest,
    ModelProjectionReadResult,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.discovery import (
    build_projection_topic_map,
    parse_order_by_clauses,
)
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

_OVERLAY_ENV = "OMNIMARKET_PROJECTION_RUNTIME_BINDING_OVERLAY"
_DECISIONS = "onex.snapshot.projection.delegation.decisions.v1"
_EMPTY = "onex.snapshot.projection.local-empty.v1"
_UNDECLARED = "onex.snapshot.projection.never-declared.v1"
_TENANT = "820272f9-4aaf-5add-a2df-0af942852ab2"
_OTHER_TENANT = "11111111-2222-4333-8444-555555555555"
_COLUMNS = ("correlation_id", "tenant_id", "written_at", "cost_usd")


def _cfg(topic: str, **overrides: Any) -> ProjectionTableConfig:
    fields: dict[str, Any] = {
        "topic": topic,
        "table": "delegation_events",
        "schema_name": "public",
        "relation_schema": "public",
        "columns": _COLUMNS,
        "order_by": "written_at DESC",
        "order_by_spec": parse_order_by_clauses("written_at DESC", _COLUMNS),
        "freshness_column": "written_at",
        "cursor_column": "written_at",
        "limit": 500,
        "bus_backed": True,
        "key_columns": ("correlation_id",),
        "tenant_column": "tenant_id",
    }
    fields.update(overrides)
    return ProjectionTableConfig(**fields)


def _topic_map() -> dict[str, ProjectionTableConfig]:
    return {
        _DECISIONS: _cfg(_DECISIONS),
        _EMPTY: _cfg(_EMPTY, table="usage_by_model_day", tenant_column=None),
    }


def _store(tmp_path: Path) -> Path:
    """Three runs for each of two tenants, and a created but empty table."""
    db_path = tmp_path / "delegation.sqlite"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE delegation_events (correlation_id TEXT NOT NULL UNIQUE, "
            "tenant_id TEXT, written_at TEXT, cost_usd REAL)"
        )
        conn.execute(
            "CREATE TABLE usage_by_model_day (correlation_id TEXT, tenant_id TEXT, "
            "written_at TEXT, cost_usd REAL)"
        )
        for tenant_index, tenant in enumerate((_TENANT, _OTHER_TENANT)):
            for i in range(3):
                conn.execute(
                    "INSERT INTO delegation_events VALUES (?, ?, ?, ?)",
                    (
                        f"19976000-0000-4000-8000-0000000{tenant_index}000{i}",
                        tenant,
                        f"2026-10-02T12:0{i}:00+00:00",
                        0.01 * (i + 1),
                    ),
                )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _client(tmp_path: Path) -> TestClient:
    handler = HandlerProjectionRead(
        topic_map=_topic_map(), row_source=SqliteTableRowSource(_store(tmp_path))
    )
    return TestClient(
        create_dashboard_app(handler=handler, topic_map=_topic_map(), tenant=_TENANT)
    )


# -- AC1: what is served --------------------------------------------------


def test_catalogue_lists_exactly_the_declared_exposures(tmp_path: Path) -> None:
    body = _client(tmp_path).get("/projections").json()
    topics = {row["topic"] for row in body["topics"]}
    assert topics == set(_topic_map())
    decisions = next(row for row in body["topics"] if row["topic"] == _DECISIONS)
    # A client classifies scoping from the catalogue, never from a 422.
    assert decisions["tenant_scoped"] is True
    assert decisions["tenant_column"] == "tenant_id"


def test_the_default_catalogue_is_the_contract_declared_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    declared = {_DECISIONS: _cfg(_DECISIONS)}
    monkeypatch.setattr(cli_dashboard, "build_projection_topic_map", lambda: declared)
    handler = HandlerProjectionRead(
        topic_map=declared, row_source=SqliteTableRowSource(_store(tmp_path))
    )
    app = create_dashboard_app(handler=handler, tenant=_TENANT)
    topics = {
        row["topic"] for row in TestClient(app).get("/projections").json()["topics"]
    }
    assert topics == {_DECISIONS}


def test_an_undeclared_topic_answers_404(tmp_path: Path) -> None:
    response = _client(tmp_path).get(f"/projection/{_UNDECLARED}")
    assert response.status_code == 404
    assert response.json()["error"] == "unknown_topic"


def test_a_declared_empty_table_answers_200_with_no_rows_and_no_as_of(
    tmp_path: Path,
) -> None:
    response = _client(tmp_path).get(f"/projection/{_EMPTY}")
    assert response.status_code == 200
    body = response.json()
    assert body["rows"] == []
    assert body["row_count"] == 0
    assert "as_of" in body
    assert body["as_of"] is None


# -- AC1: tenant -------------------------------------------------------------


def test_a_scoped_read_is_served_the_local_tenant_only(tmp_path: Path) -> None:
    response = _client(tmp_path).get(f"/projection/{_DECISIONS}")
    assert response.status_code == 200
    rows = response.json()["rows"]
    assert len(rows) == 3
    assert {row["tenant_id"] for row in rows} == {_TENANT}
    assert response.json()["as_of"] == "2026-10-02T12:02:00+00:00"


def test_naming_the_local_tenant_is_served(tmp_path: Path) -> None:
    response = _client(tmp_path).get(f"/projection/{_DECISIONS}?tenant={_TENANT}")
    assert response.status_code == 200
    assert len(response.json()["rows"]) == 3


def test_another_tenants_rows_answer_422(tmp_path: Path) -> None:
    response = _client(tmp_path).get(f"/projection/{_DECISIONS}?tenant={_OTHER_TENANT}")
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "tenant_conflict"
    assert "rows" not in body


def test_an_install_without_an_identity_refuses_scoped_reads(tmp_path: Path) -> None:
    handler = HandlerProjectionRead(
        topic_map=_topic_map(), row_source=SqliteTableRowSource(_store(tmp_path))
    )
    client = TestClient(
        create_dashboard_app(handler=handler, topic_map=_topic_map(), tenant=None)
    )
    response = client.get(f"/projection/{_DECISIONS}")
    # Never served unscoped: the read is refused by name.
    assert response.status_code == 422


# -- AC1: SQL stays in the adapter, reads go through the node ------------------


def _code_strings(source: str) -> list[str]:
    """Every string literal in the module except docstrings."""
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


_SQL = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE|PRAGMA)\b", re.IGNORECASE)


def test_no_sql_text_lives_in_the_serving_shim() -> None:
    source = Path(cli_dashboard.__file__).read_text(encoding="utf-8")
    assert [s for s in _code_strings(source) if _SQL.search(s)] == []
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (
            node.names
            if isinstance(node, ast.Import)
            else [ast.alias(node.module or "")]
        )
    }
    assert imported.isdisjoint(
        {"sqlite3", "asyncpg", "psycopg", "psycopg2", "sqlalchemy"}
    )


def test_the_sql_check_catches_sql_in_a_string() -> None:
    # Positive control: the check above must fire on SQL it is meant to catch.
    planted = 'QUERY = "SELECT correlation_id FROM delegation_events"\n'
    assert [s for s in _code_strings(planted) if _SQL.search(s)] != []


def test_every_read_is_dispatched_to_the_read_node() -> None:
    seen: list[ModelProjectionReadRequest] = []

    class _Node:
        async def handle(
            self, request: ModelProjectionReadRequest
        ) -> ModelProjectionReadResult:
            seen.append(request)
            return ModelProjectionReadResult(
                topic=request.topic,
                ok=True,
                http_status=200,
                row_count=0,
                rows=[],
                tenant=request.tenant_id,
                next_cursor=None,
                truncated=False,
                response={"rows": [], "row_count": 0, "latest_event_at": None},
            )

    node: ProtocolProjectionReadNode = _Node()
    app = create_dashboard_app(handler=node, topic_map=_topic_map(), tenant=_TENANT)
    TestClient(app).get(f"/projection/{_DECISIONS}?limit=5")
    assert [(r.topic, r.tenant_id, r.limit) for r in seen] == [(_DECISIONS, _TENANT, 5)]


# -- the node's handle() ---------------------------------------------------------


def test_the_node_serves_the_declared_catalogue_and_reports_what_it_served(
    tmp_path: Path,
) -> None:
    served: list[tuple[FastAPI, str, int]] = []

    async def serve(app: FastAPI, host: str, port: int) -> None:
        served.append((app, host, port))

    handler = HandlerLocalDashboardServe(
        topic_map=_topic_map(),
        row_source=SqliteTableRowSource(_store(tmp_path)),
        serve=serve,
    )
    result = asyncio.run(
        handler.handle(
            ModelLocalDashboardServeRequest(
                host="127.0.0.1", port=7600, tenant_id=_TENANT
            )
        )
    )
    assert result.url == "http://127.0.0.1:7600"
    assert result.exposure_count == 2
    assert result.tenant_id == _TENANT
    [(app, host, port)] = served
    assert (host, port) == ("127.0.0.1", 7600)
    rows = TestClient(app).get(f"/projection/{_DECISIONS}").json()["rows"]
    assert {row["tenant_id"] for row in rows} == {_TENANT}


@pytest.mark.parametrize(("host", "port"), [("127.0.0.1", 3002), ("203.0.113.7", 7600)])
def test_a_serve_request_refuses_3002_and_non_loopback(host: str, port: int) -> None:
    with pytest.raises(ValidationError, match=r"dashboard\.bind"):
        ModelLocalDashboardServeRequest(host=host, port=port)


# -- dashboard.bind ------------------------------------------------------------


def test_bind_defaults_to_loopback_on_a_free_port() -> None:
    assert resolve_dashboard_bind({}) == ("127.0.0.1", 0)
    assert resolve_dashboard_bind(None) == ("127.0.0.1", 0)


def test_bind_comes_from_the_dashboard_bind_overlay_key() -> None:
    assert resolve_dashboard_bind({"dashboard": {"bind": "127.0.0.1:7600"}}) == (
        "127.0.0.1",
        7600,
    )
    assert resolve_dashboard_bind({"dashboard": {"bind": "localhost:7601"}}) == (
        "localhost",
        7601,
    )


@pytest.mark.parametrize(
    "bind",
    [
        "127.0.0.1:3002",
        "0.0.0.0:7600",
        "203.0.113.7:7600",
        "127.0.0.1",
        "127.0.0.1:x",
    ],
)
def test_bind_refuses_3002_non_loopback_and_malformed_values(bind: str) -> None:
    with pytest.raises(DashboardBindError):
        resolve_dashboard_bind({"dashboard": {"bind": bind}})


# -- the store the local writers fill ------------------------------------------


def test_no_binding_reads_the_local_writers_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(_OVERLAY_ENV, raising=False)
    store = tmp_path / "delegation.sqlite"
    monkeypatch.setattr(cli_dashboard, "default_evidence_db_path", lambda: store)
    source = resolve_local_row_source()
    assert isinstance(source, SqliteTableRowSource)
    assert source.db_path == store


def test_a_configured_binding_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _store(tmp_path)
    overlay = tmp_path / "projection_binding.yaml"
    overlay.write_text(
        f"kafka_bootstrap_servers: inmemory\ndatabase_url: 'sqlite:///{store}'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(_OVERLAY_ENV, str(overlay))
    monkeypatch.setattr(
        cli_dashboard, "default_evidence_db_path", lambda: tmp_path / "unused.sqlite"
    )
    source = resolve_local_row_source()
    assert isinstance(source, SqliteTableRowSource)
    assert source.db_path == store


# -- the command exists ----------------------------------------------------------


def test_onex_dashboard_is_registered_in_the_onex_cli_group() -> None:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    entries = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"][
        "entry-points"
    ]["onex.cli"]
    assert entries["dashboard"] == "omnimarket.cli.cli_local:dashboard_command"


def test_the_shim_never_starts_the_standalone_api() -> None:
    source = Path(cli_dashboard.__file__).read_text(encoding="utf-8")
    assert not re.search(r"projection\.api_server", source)
    assert 'uvicorn.run("omnimarket' not in source


# -- AC1 on a store the real writer created ---------------------------------------


class _NullPublisher:
    """No broker here; the snapshot republish is not what this case proves."""

    def publish(self, *args: object, **kwargs: object) -> bool:
        return True


def test_a_store_the_real_writer_created_serves_runs_with_every_declared_column(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    HandlerProjectionDelegation(publisher=_NullPublisher()).handle(
        {
            "status": "completed",
            "correlation_id": "19976000-0000-4000-8000-0000000003a1",
            "task_type": "research",
            # A slug the writer resolves without a tenant registry row; the
            # dashboard serves the UUID the writer stored, as `onex local init`
            # would mint it.
            "tenant_id": "omninode",
            "metrics": {"cost_usd": 0.0},
            "timestamp": "2026-10-03T12:00:00+00:00",
            "_db": SqliteDatabaseAdapter(db_path),
        }
    )
    conn = sqlite3.connect(db_path)
    try:
        (tenant,) = conn.execute("SELECT tenant_id FROM delegation_events").fetchone()
    finally:
        conn.close()
    cfg = build_projection_topic_map()[_DECISIONS]
    topics = {_DECISIONS: cfg}
    handler = HandlerProjectionRead(
        topic_map=topics, row_source=SqliteTableRowSource(db_path)
    )
    client = TestClient(
        create_dashboard_app(handler=handler, topic_map=topics, tenant=str(tenant))
    )
    response = client.get(f"/projection/{_DECISIONS}")
    assert response.status_code == 200, response.json()
    rows = response.json()["rows"]
    assert [row["correlation_id"] for row in rows] == [
        "19976000-0000-4000-8000-0000000003a1"
    ]
    assert set(rows[0]) == set(cfg.columns)
