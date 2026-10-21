# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20732: a store written before the run-lineage columns serves the decisions read.

delegation.decisions.v1 declares parent_correlation_id, lineage_kind and
parent_failure_cause. A delegation store an earlier build wrote has none of them,
and the read node opens the store read-only, so it would refuse the exposure with
projection_column_missing (HTTP 503) until some write added them. Failure modes,
each with a test below:

L1  the real ``onex dashboard`` startup answers the decisions read of such a store
    with HTTP 200 and every row the store holds;
L2  each served row carries the three lineage keys, null for rows written before
    them, never dropped from the response shape.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from omnimarket.projection import sqlite_database
from tests.nodes.node_projection_metering_summary.test_omn20226_compression_and_cache_hit import (
    TENANT,
)

pytestmark = pytest.mark.unit

DECISIONS = "onex.snapshot.projection.delegation.decisions.v1"
LINEAGE = ("parent_correlation_id", "lineage_kind", "parent_failure_cause")
SEEDED = ("corr-pre-lineage-1", "corr-pre-lineage-2")


def _pre_lineage_store(db_path: Path) -> None:
    """delegation_events as a build before the lineage columns wrote it."""
    columns = [
        column
        for column in sqlite_database._DELEGATION_EVENTS_DECLARED_COLUMNS
        if column not in LINEAGE and column != "correlation_id"
    ]
    assert not set(LINEAGE) & set(columns)
    ddl = (
        "CREATE TABLE delegation_events (id INTEGER PRIMARY KEY, "
        "correlation_id TEXT NOT NULL UNIQUE, " + ", ".join(columns) + ")"
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(ddl)
        for index, correlation_id in enumerate(SEEDED, start=1):
            stamp = f"2026-10-0{index}T00:00:00+00:00"
            conn.execute(
                "INSERT INTO delegation_events (correlation_id, tenant_id, created_at,"
                " written_at, task_type, model_name, data_source)"
                " VALUES (?, ?, ?, ?, 'summarization', 'Qwen3.8-27B', 'real')",
                (correlation_id, TENANT, stamp, stamp),
            )
    with sqlite3.connect(db_path) as conn:
        names = {row[1] for row in conn.execute("PRAGMA table_info(delegation_events)")}
    assert not set(LINEAGE) & names


def _startup_decisions_reply(
    monkeypatch: pytest.MonkeyPatch, db_path: Path, pages: Path
) -> Any:
    """GET decisions.v1 from the app ``onex dashboard`` would start."""
    from omnimarket.nodes.node_local_dashboard_serve_effect.handlers import (
        handler_local_dashboard_serve as dashboard,
    )
    from omnimarket.nodes.node_local_dashboard_serve_effect.models import (
        ModelLocalDashboardServeRequest,
    )

    monkeypatch.setattr(dashboard, "default_evidence_db_path", lambda: db_path)
    replies: list[Any] = []
    request = ModelLocalDashboardServeRequest(
        host="127.0.0.1", port=8123, tenant_id=TENANT
    )

    async def capture(app: Any, host: str, port: int) -> None:
        replies.append(
            TestClient(app, base_url=f"http://{host}:{port}").get(
                f"/projection/{DECISIONS}",
                params={"tenant": TENANT},
                headers={
                    "Authorization": f"Bearer {request.access_token.get_secret_value()}"
                },
            )
        )

    asyncio.run(
        dashboard.HandlerLocalDashboardServe(serve=capture, pages=pages).handle(request)
    )
    [reply] = replies
    return reply


def test_l1_dashboard_startup_serves_a_pre_lineage_store_with_its_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    _pre_lineage_store(db_path)
    reply = _startup_decisions_reply(monkeypatch, db_path, tmp_path)
    assert reply.status_code == 200, reply.json()
    served = {row["correlation_id"] for row in reply.json()["rows"]}
    assert served == set(SEEDED), served


def test_l2_every_served_row_carries_the_lineage_keys_as_null(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    _pre_lineage_store(db_path)
    reply = _startup_decisions_reply(monkeypatch, db_path, tmp_path)
    assert reply.status_code == 200, reply.json()
    rows = reply.json()["rows"]
    assert rows
    for row in rows:
        for key in LINEAGE:
            assert key in row, (key, sorted(row))
            assert row[key] is None, (key, row[key])
