# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Runs exposure serves each run's cost from the local store (OMN-19984).

C30 claims a developer sees a delegation's model, tokens, cost and receipt on
the local dashboard, and the ticket grades that claim on the Runs exposure
(``onex.snapshot.projection.delegation.decisions.v1``). Each test names the
failure it exists to catch:

* Runs declares no ``cost_usd``, so a run the real writer priced is served
  without its cost and the dashboard cannot show it;
* the cost served is not the cost the writer stored for that correlation id;
* a free local run is served with a null cost, where the writer stores a
  measured zero (a null would read as "unmeasured", which it is not).

Every read goes through the real ``onex dashboard`` app and the contract's
own exposure, against a SQLite store the real delegation writer created.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omnimarket.nodes.node_local_dashboard_serve_effect.handlers.handler_local_dashboard_serve import (
    create_dashboard_app,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

_RUNS = "onex.snapshot.projection.delegation.decisions.v1"


class _NullPublisher:
    """No broker here; the snapshot republish is not what these cases prove."""

    def publish(self, *args: object, **kwargs: object) -> bool:
        return True


def _write_run(db_path: Path, correlation_id: str, cost_usd: float) -> str:
    """One completed delegation through the real writer; returns its stored tenant."""
    HandlerProjectionDelegation(publisher=_NullPublisher()).handle(
        {
            "status": "completed",
            "correlation_id": correlation_id,
            "task_type": "research",
            "tenant_id": "omninode",
            "metrics": {"cost_usd": cost_usd},
            "timestamp": "2026-10-05T12:00:00+00:00",
            "_db": SqliteDatabaseAdapter(db_path),
        }
    )
    conn = sqlite3.connect(db_path)
    try:
        (tenant,) = conn.execute(
            "SELECT tenant_id FROM delegation_events WHERE correlation_id = ?",
            (correlation_id,),
        ).fetchone()
    finally:
        conn.close()
    return str(tenant)


def _stored_cost(db_path: Path, correlation_id: str) -> object:
    conn = sqlite3.connect(db_path)
    try:
        (cost,) = conn.execute(
            "SELECT cost_usd FROM delegation_events WHERE correlation_id = ?",
            (correlation_id,),
        ).fetchone()
    finally:
        conn.close()
    return cost


def _served_run(db_path: Path, tenant: str, correlation_id: str) -> dict[str, object]:
    """The Runs row for one correlation id, as `onex dashboard` serves it."""
    topics = {_RUNS: build_projection_topic_map()[_RUNS]}
    handler = HandlerProjectionRead(
        topic_map=topics, row_source=SqliteTableRowSource(db_path)
    )
    client = TestClient(
        create_dashboard_app(handler=handler, topic_map=topics, tenant=tenant)
    )
    response = client.get(
        f"/projection/{_RUNS}", params={"correlation_id": correlation_id}
    )
    assert response.status_code == 200, response.json()
    rows = response.json()["rows"]
    assert [row["correlation_id"] for row in rows] == [correlation_id]
    return rows[0]


def test_runs_declares_cost_usd() -> None:
    assert "cost_usd" in build_projection_topic_map()[_RUNS].columns


def test_a_priced_run_is_served_on_runs_with_the_cost_the_writer_stored(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    correlation_id = "19984000-0000-4000-8000-0000000000c1"
    tenant = _write_run(db_path, correlation_id, 0.0123)
    stored = _stored_cost(db_path, correlation_id)
    assert stored == pytest.approx(0.0123)
    row = _served_run(db_path, tenant, correlation_id)
    assert "cost_usd" in row, "Runs served the run without its cost"
    assert float(row["cost_usd"]) == pytest.approx(float(stored))


def test_a_free_local_run_is_served_a_measured_zero_not_null(tmp_path: Path) -> None:
    db_path = tmp_path / "delegation.sqlite"
    correlation_id = "19984000-0000-4000-8000-0000000000c0"
    tenant = _write_run(db_path, correlation_id, 0.0)
    assert _stored_cost(db_path, correlation_id) == 0
    row = _served_run(db_path, tenant, correlation_id)
    assert "cost_usd" in row, "Runs served the run without its cost"
    assert row["cost_usd"] is not None
    assert float(row["cost_usd"]) == 0.0
