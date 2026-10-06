# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local SQLite store carries every column the delegation exposures declare.

``onex dashboard`` serves the Runs page from the local SQLite store through the
projection read node, and the read node refuses an exposure whose declared
column the relation lacks (``projection_column_missing``). The store used to
create ``delegation_events`` with ``correlation_id`` alone and add a column only
when a written row carried it, so a column the local writer never sends never
existed. A store created fresh lacked 8 of the 28 columns Runs declares.

Each test names the failure it exists to catch:

* a fresh store, after one real delegation write, cannot serve Runs, or serves
  it with ``id`` NULL where Postgres serves an integer;
* a store created before this change keeps lacking the columns, or the upgrade
  loses or rewrites a row it already holds, or leaves ``id`` unfilled or
  duplicated, including for rows written after the upgrade;
* a column a delegation exposure declares is missing from the store, so the
  next exposure or migration that adds one drifts silently.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.models import (
    ModelProjectionReadRequest,
)
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

_OVERLAY_ENV = "OMNIMARKET_PROJECTION_RUNTIME_BINDING_OVERLAY"
_DECISIONS = "onex.snapshot.projection.delegation.decisions.v1"
_TRACE = "onex.snapshot.projection.delegation.correlation-trace.v1"
_TABLE = "delegation_events"
# A slug the writer resolves without a tenant registry row, as the OMN-19968
# parity fixtures do; the read uses the UUID the writer actually stored.
_TENANT_SLUG = "omninode"


class _NullPublisher:
    """No broker here; the snapshot republish is not what this module proves."""

    def publish(self, *args: object, **kwargs: object) -> bool:
        return True


@pytest.fixture(scope="module")
def delegation_exposures() -> dict[str, ProjectionTableConfig]:
    exposures = {
        topic: cfg
        for topic, cfg in build_projection_topic_map().items()
        if cfg.table == _TABLE
    }
    assert _DECISIONS in exposures, "the Runs exposure is no longer discovered"
    return exposures


def _event(correlation_id: str, minute: int) -> dict[str, Any]:
    return {
        "status": "completed",
        "correlation_id": correlation_id,
        "task_type": "research",
        "tenant_id": _TENANT_SLUG,
        "metrics": {"cost_usd": 0.0},
        "timestamp": f"2026-10-03T12:0{minute}:00+00:00",
    }


def _write(adapter: SqliteDatabaseAdapter, correlation_id: str, minute: int) -> None:
    HandlerProjectionDelegation(publisher=_NullPublisher()).handle(
        {**_event(correlation_id, minute), "_db": adapter}
    )


def _columns(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({_TABLE})")}
    finally:
        conn.close()


def _rows(db_path: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(row)
            for row in conn.execute(f"SELECT * FROM {_TABLE} ORDER BY correlation_id")
        ]
    finally:
        conn.close()


def _bind(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, db_path: Path) -> None:
    overlay = tmp_path / "projection_binding.yaml"
    overlay.write_text(
        f"kafka_bootstrap_servers: inmemory\ndatabase_url: 'sqlite:///{db_path}'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(_OVERLAY_ENV, str(overlay))


async def test_a_fresh_store_serves_runs_with_every_declared_column(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    delegation_exposures: dict[str, ProjectionTableConfig],
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    _write(SqliteDatabaseAdapter(db_path), "19976000-0000-4000-8000-000000000001", 0)
    (stored,) = _rows(db_path)
    _bind(monkeypatch, tmp_path, db_path)
    cfg = delegation_exposures[_DECISIONS]
    handler = HandlerProjectionRead(topic_map={_DECISIONS: cfg})
    try:
        result = await handler.handle(
            ModelProjectionReadRequest(
                topic=_DECISIONS, tenant_id=str(stored["tenant_id"])
            )
        )
    finally:
        await handler.close()
    assert result.ok is True, result
    assert [row["correlation_id"] for row in result.rows] == [
        "19976000-0000-4000-8000-000000000001"
    ]
    assert set(result.rows[0]) == set(cfg.columns)
    assert isinstance(result.rows[0]["id"], int), result.rows[0]["id"]


def test_an_existing_narrow_store_is_upgraded_without_touching_its_rows(
    tmp_path: Path,
    delegation_exposures: dict[str, ProjectionTableConfig],
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    conn = sqlite3.connect(db_path)
    try:
        # The table exactly as the store created it before this change, widened
        # only by the columns a write added.
        conn.execute(f"CREATE TABLE {_TABLE} (correlation_id TEXT NOT NULL UNIQUE)")
        conn.execute(f"ALTER TABLE {_TABLE} ADD COLUMN tenant_id")
        conn.execute(f"ALTER TABLE {_TABLE} ADD COLUMN cost_usd")
        conn.executemany(
            f"INSERT INTO {_TABLE} VALUES (?, ?, ?)",
            [
                (f"19976000-0000-4000-8000-00000000010{i}", _TENANT_SLUG, 0.25 * i)
                for i in range(3)
            ],
        )
        conn.commit()
    finally:
        conn.close()
    before = _rows(db_path)

    adapter = SqliteDatabaseAdapter(db_path)
    adapter.query(_TABLE)
    adapter.query(_TABLE)  # a second open changes nothing
    after = _rows(db_path)

    declared = set().union(*(cfg.columns for cfg in delegation_exposures.values()))
    assert declared <= _columns(db_path)
    assert len(after) == len(before)
    for old, new in zip(before, after, strict=True):
        assert {key: new[key] for key in old} == old
    ids = [row["id"] for row in after]
    assert all(isinstance(value, int) for value in ids), ids
    assert len(set(ids)) == len(ids), ids

    _write(adapter, "19976000-0000-4000-8000-000000000201", 1)
    written = [
        row["id"]
        for row in _rows(db_path)
        if row["correlation_id"] == "19976000-0000-4000-8000-000000000201"
    ]
    assert len(written) == 1
    assert isinstance(written[0], int)
    assert written[0] not in ids


def test_every_declared_delegation_column_exists_in_a_fresh_store(
    tmp_path: Path,
    delegation_exposures: dict[str, ProjectionTableConfig],
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    SqliteDatabaseAdapter(db_path).query(_TABLE)
    present = _columns(db_path)
    missing = {
        topic: sorted(set(cfg.columns) - present)
        for topic, cfg in delegation_exposures.items()
        if set(cfg.columns) - present
    }
    assert missing == {}


def _open_together(db_path: Path, workers: int) -> list[BaseException]:
    barrier = threading.Barrier(workers)
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def open_store() -> None:
        barrier.wait()
        try:
            SqliteDatabaseAdapter(db_path).query(_TABLE)
        except BaseException as exc:
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=open_store) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return errors


def test_concurrent_first_opens_of_one_store_all_succeed(tmp_path: Path) -> None:
    # The delegate orchestrator runs several records in flight in one process,
    # so the first opens of a fresh store arrive together. Each must succeed:
    # two connections that both see a column missing must not both add it.
    for attempt in range(20):
        db_path = tmp_path / f"delegation-{attempt}.sqlite"
        assert _open_together(db_path, workers=8) == []
        assert _columns(db_path) >= {"id", "correlation_id", "trace_id"}


async def test_a_fresh_store_serves_correlation_trace_tenant_scoped_with_cost(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    delegation_exposures: dict[str, ProjectionTableConfig],
) -> None:
    """The per-correlation detail exposure is served, not refused as
    ``not_yet_bus_backed``, carries ``cost_usd``, and is scoped to its tenant."""
    correlation_id = "19976000-0000-4000-8000-000000000301"
    db_path = tmp_path / "delegation.sqlite"
    adapter = SqliteDatabaseAdapter(db_path)
    HandlerProjectionDelegation(publisher=_NullPublisher()).handle(
        {
            **_event(correlation_id, 3),
            "metrics": {"cost_usd": 0.37},
            "_db": adapter,
        }
    )
    (stored,) = _rows(db_path)
    _bind(monkeypatch, tmp_path, db_path)
    cfg = delegation_exposures[_TRACE]
    assert cfg.bus_backed is True
    assert cfg.tenant_scoped is True
    handler = HandlerProjectionRead(topic_map={_TRACE: cfg})
    try:
        served = await handler.handle(
            ModelProjectionReadRequest(
                topic=_TRACE,
                tenant_id=str(stored["tenant_id"]),
                row_correlation_id=correlation_id,
            )
        )
        other = await handler.handle(
            ModelProjectionReadRequest(
                topic=_TRACE,
                tenant_id="00000000-0000-4000-8000-00000000dead",
                row_correlation_id=correlation_id,
            )
        )
    finally:
        await handler.close()
    assert served.ok is True, served
    assert [row["correlation_id"] for row in served.rows] == [correlation_id]
    assert served.rows[0]["cost_usd"] == pytest.approx(0.37)
    assert not other.rows, other
