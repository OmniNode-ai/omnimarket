# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20226: an existing local store serves metering_summary's new columns.

The three columns (compression_ratio, cache_hit_rate, runs_cache_answered) are
new, and CREATE TABLE IF NOT EXISTS leaves a table an earlier build wrote
unchanged, so the read node would refuse the whole metering-summary.v1
exposure with projection_column_missing. Failure modes, each with a test below:

C9  a local SQLite store written before these columns existed serves them as
    null as soon as it is opened, keeping its rows;
C10 opening that upgraded store again changes nothing: each column exists once;
C11 the real ``onex dashboard`` startup (HandlerLocalDashboardServe with its own
    store resolution) upgrades that store before serving, rather than relying on
    some writer having opened it first;
C12 a store the dashboard cannot write is served as it is: startup does not
    crash, the exposure still refuses honestly, and the file is unchanged;
C13 with no store yet, startup does not create one.
"""

from __future__ import annotations

import asyncio
import sqlite3
import stat
from pathlib import Path
from typing import Any

import pytest
import yaml
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
from tests.nodes.node_projection_metering_summary.test_omn20226_compression_and_cache_hit import (
    FIELDS,
    METERING,
    TENANT,
    _fold,
)

pytestmark = pytest.mark.unit


def _old_store(db_path: Path) -> dict[str, Any]:
    """A store as a build before the three columns wrote it, holding one row."""
    from omnimarket.projection import sqlite_database

    ddl = sqlite_database._METERING_SUMMARY_DDL
    for field in FIELDS:
        [line] = [line for line in ddl.splitlines() if line.strip().startswith(field)]
        ddl = ddl.replace(line + "\n", "")
    assert not any(field in ddl for field in FIELDS)
    row = next(iter(_fold().values())).model_dump(mode="json")
    for field in FIELDS:
        row.pop(field)
    stored = {
        key: (yaml.safe_dump(value) if isinstance(value, dict | list) else value)
        for key, value in row.items()
        if key in ddl
    }
    with sqlite3.connect(db_path) as conn:
        conn.execute(ddl)
        columns = ", ".join(stored)
        marks = ", ".join("?" for _ in stored)
        conn.execute(
            f"INSERT INTO metering_summary ({columns}) VALUES ({marks})",
            tuple(stored.values()),
        )
    return stored


def test_c9_an_old_store_serves_the_three_fields_as_null_once_opened(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    stored = _old_store(db_path)
    # The local runtime opens the store before it serves anything.
    SqliteDatabaseAdapter(db_path).query("metering_summary", {"tenant_id": TENANT})
    topics = build_projection_topic_map()
    handler = HandlerProjectionRead(
        topic_map=topics, row_source=SqliteTableRowSource(db_path)
    )
    client = TestClient(
        create_dashboard_app(handler=handler, topic_map=topics, tenant=TENANT)
    )
    response = client.get(f"/projection/{METERING}")
    assert response.status_code == 200, response.json()
    [served] = response.json()["rows"]
    for field in FIELDS:
        assert field in served, field
        assert served[field] is None, (field, served[field])
    assert served["runs_total"] == stored["runs_total"]
    assert served["window_kind"] == stored["window_kind"]


def test_c10_reopening_the_upgraded_store_changes_nothing(tmp_path: Path) -> None:
    db_path = tmp_path / "delegation.sqlite"
    _old_store(db_path)
    for _ in range(3):
        SqliteDatabaseAdapter(db_path).query("metering_summary", {"tenant_id": TENANT})
    with sqlite3.connect(db_path) as conn:
        names = [row[1] for row in conn.execute("PRAGMA table_info(metering_summary)")]
        count = conn.execute("SELECT COUNT(*) FROM metering_summary").fetchone()[0]
    for field in FIELDS:
        assert names.count(field) == 1, (field, names)
    assert count == 1


def _startup_reply(monkeypatch: pytest.MonkeyPatch, db_path: Path, pages: Path) -> Any:
    """GET metering-summary.v1 from the app ``onex dashboard`` would start."""
    from omnimarket.nodes.node_local_dashboard_serve_effect.handlers import (
        handler_local_dashboard_serve as dashboard,
    )
    from omnimarket.nodes.node_local_dashboard_serve_effect.models import (
        ModelLocalDashboardServeRequest,
    )

    monkeypatch.setattr(dashboard, "default_evidence_db_path", lambda: db_path)
    replies: list[Any] = []

    async def capture(app: Any, _host: str, _port: int) -> None:
        replies.append(TestClient(app).get(f"/projection/{METERING}"))

    asyncio.run(
        dashboard.HandlerLocalDashboardServe(serve=capture, pages=pages).handle(
            ModelLocalDashboardServeRequest(
                host="127.0.0.1", port=8123, tenant_id=TENANT
            )
        )
    )
    [reply] = replies
    return reply


def test_c11_dashboard_startup_upgrades_an_old_store_before_serving(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    stored = _old_store(db_path)
    reply = _startup_reply(monkeypatch, db_path, tmp_path)
    assert reply.status_code == 200, reply.json()
    [served] = reply.json()["rows"]
    for field in FIELDS:
        assert served[field] is None, (field, served[field])
    assert served["runs_total"] == stored["runs_total"]


def test_c12_a_store_the_dashboard_cannot_write_is_served_as_it_is(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    _old_store(db_path)
    db_path.chmod(stat.S_IRUSR)
    before = db_path.read_bytes()
    try:
        reply = _startup_reply(monkeypatch, db_path, tmp_path)
    finally:
        db_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert reply.status_code == 503
    assert reply.json()["error"] == "projection_column_missing"
    assert db_path.read_bytes() == before


def test_c13_startup_with_no_store_creates_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    _startup_reply(monkeypatch, db_path, tmp_path)
    assert not db_path.exists()
