# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The decisions exposure serves each run's terminal status and cost (OMN-20753).

The OMN-20223 walk found Runs showing Status and Cost "Not recorded" on all 23
runs of a fresh install, while every receipt said ``success`` and $0.00 and the
local store held ``terminal_ok = 1`` and ``cost_usd = 0.0``. The decisions
exposure, the read Runs is built from, declared neither column, so the page was
never sent them. Both columns exist on ``delegation_events`` in Postgres
(``terminal_ok``: migration 0029; ``cost_usd`` is already served by the
``delegation`` exposure on the same table) and in the local store.

The store here is the one a fresh install has after ``onex delegate``: a SQLite
file opened by the writer's adapter, read through the real ``onex dashboard``
app and the real, contract-declared topic map.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient

from omnimarket.nodes.node_local_dashboard_serve_effect.handlers.handler_local_dashboard_serve import (
    create_dashboard_app,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

_TENANT = "bc08ff01-9d50-442c-aca1-7ad2e687db70"
_DECISIONS = "onex.snapshot.projection.delegation.decisions.v1"
_MEASURED = "839e44bf-410a-4686-8a6e-dca9559a4fc5"
_UNMEASURED = "5fa3947b-c41d-488a-a6f6-15b0776efc32"


def _store(tmp_path: Path) -> Path:
    path = tmp_path / "delegation.sqlite"
    adapter = SqliteDatabaseAdapter(path)
    for correlation_id, cost, written_at in (
        (_MEASURED, 0.0, "2026-10-08T18:40:44+00:00"),
        (_UNMEASURED, None, "2026-10-08T18:40:18+00:00"),
    ):
        adapter.upsert(
            "delegation_events",
            "correlation_id",
            {
                "correlation_id": correlation_id,
                "tenant_id": _TENANT,
                "task_type": "document",
                "delegated_to": "gemini-3.5-flash-lite",
                "quality_gate_passed": True,
                "quality_gate_detail": "completed",
                "terminal_ok": True,
                "cost_usd": cost,
                "data_source": "real",
                "created_at": written_at,
                "written_at": written_at,
            },
        )
    return path


# Built once: discovering every contract's exposures takes seconds, and the
# repo-evidence check runs this file under a 30-second budget.
_TOPICS = build_projection_topic_map()


def _rows(tmp_path: Path) -> dict[str, dict[str, object]]:
    topics = _TOPICS
    source = SqliteTableRowSource(_store(tmp_path))
    client = TestClient(
        create_dashboard_app(
            handler=HandlerProjectionRead(topic_map=topics, row_source=source),
            tenant=_TENANT,
            topic_map=topics,
        )
    )
    response = client.get(f"/projection/{_DECISIONS}?tenant={quote(_TENANT)}")
    assert response.status_code == 200, response.text
    return {str(row["correlation_id"]): row for row in response.json()["rows"]}


def test_the_decisions_row_carries_terminal_status_and_cost(tmp_path: Path) -> None:
    row = _rows(tmp_path)[_MEASURED]
    assert "terminal_ok" in row
    assert "cost_usd" in row
    assert bool(row["terminal_ok"]) is True
    assert row["cost_usd"] == 0.0


def test_an_unmeasured_cost_is_served_as_null_not_zero(tmp_path: Path) -> None:
    row = _rows(tmp_path)[_UNMEASURED]
    assert "cost_usd" in row
    assert row["cost_usd"] is None
