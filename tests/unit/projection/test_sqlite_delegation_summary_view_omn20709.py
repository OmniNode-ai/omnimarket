# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A local store serves the delegation summary exposure (OMN-20709).

On Postgres ``projection_delegation_summary`` is a view over
``delegation_events`` (node_projection_delegation migration 0050). A local
store had no such relation, so ``onex dashboard`` listed the exposure ``ok`` and
answered every read of it 503 ``projection_table_missing``.
``SqliteDatabaseAdapter`` now creates the SQLite counterpart as a one-time store
step.

Each test names the failure it exists to catch:

* one recorded delegation still reads 503, or 200 with no row (AC1);
* the view counts another tenant's rows, or serves them to this one;
* fixture savings are added to the headline savings, which migration 0050 forbids;
* a row with no quality verdict is counted as failed;
* ``byTaskType`` and ``byModel`` come back as JSON text instead of lists;
* a store created before the step never gains the view.

Every store is a SQLite file under pytest's ``tmp_path``. Reads go through the
real read node over :class:`SqliteTableRowSource`, as ``onex dashboard`` does.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.model_projection_read import ModelProjectionReadRequest
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit

_SUMMARY = "onex.snapshot.projection.delegation.summary.v1"
_TENANT = "0af28cd0-0654-42e0-a050-358ee8240940"
_OTHER = "11111111-2222-4333-8444-555555555555"


def _delegation(correlation_id: str, **fields: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "correlation_id": correlation_id,
        "tenant_id": _TENANT,
        "task_type": "reasoning",
        "delegated_to": "gemini-3.5-flash-lite",
        "quality_gate_passed": True,
        "latency_ms": 900,
        "cost_savings_usd": 0.25,
        "data_source": "real",
        "created_at": "2026-10-08T03:11:00.500000+00:00",
    }
    row.update(fields)
    return row


def _store(tmp_path: Path, *rows: dict[str, Any]) -> Path:
    path = tmp_path / "delegation.sqlite"
    adapter = SqliteDatabaseAdapter(path)
    for row in rows:
        adapter.upsert("delegation_events", "correlation_id", row)
    return path


def _read(path: Path, tenant: str = _TENANT) -> tuple[int, dict[str, Any]]:
    topics = build_projection_topic_map()
    handler = HandlerProjectionRead(
        topic_map=topics, row_source=SqliteTableRowSource(path)
    )
    result = asyncio.run(
        handler.handle(ModelProjectionReadRequest(topic=_SUMMARY, tenant_id=tenant))
    )
    return result.http_status, dict(result.response or {})


def test_one_recorded_delegation_reads_200_with_a_summary_row(tmp_path: Path) -> None:
    status, body = _read(_store(tmp_path, _delegation("b4030008")))
    assert status == 200, body
    assert body["row_count"] == 1
    row = body["rows"][0]
    assert row["totalDelegations"] >= 1
    assert row["tenant_id"] == _TENANT


def test_the_summary_counts_only_the_requested_tenant(tmp_path: Path) -> None:
    path = _store(
        tmp_path,
        _delegation("a"),
        _delegation("b"),
        _delegation("c", tenant_id=_OTHER),
    )
    _, mine = _read(path)
    _, theirs = _read(path, _OTHER)
    assert mine["rows"][0]["totalDelegations"] == 2
    assert theirs["rows"][0]["totalDelegations"] == 1


def test_fixture_savings_stay_out_of_the_headline_savings(tmp_path: Path) -> None:
    path = _store(
        tmp_path,
        _delegation("real", cost_savings_usd=0.25),
        _delegation("fixture", cost_savings_usd=10.0, data_source="fixture"),
    )
    row = _read(path)[1]["rows"][0]
    assert row["totalSavingsUsd"] == pytest.approx(0.25)
    assert row["fixtureSavingsUsd"] == pytest.approx(10.0)
    assert row["fixtureDelegations"] == 1
    assert row["totalDelegations"] == 2


def test_a_row_with_no_quality_verdict_is_not_counted_as_failed(
    tmp_path: Path,
) -> None:
    path = _store(
        tmp_path,
        _delegation("passed", quality_gate_passed=True),
        _delegation("failed", quality_gate_passed=False),
        _delegation("unchecked", quality_gate_passed=None),
    )
    row = _read(path)[1]["rows"][0]
    assert row["qualityGatePassed"] == 1
    assert row["quality_failed_count"] == 1
    assert row["qualityGateTotal"] == 2
    assert row["qualityGatePassRate"] == pytest.approx(0.5)


def test_the_breakdowns_are_lists_ordered_by_count(tmp_path: Path) -> None:
    path = _store(
        tmp_path,
        _delegation("1", task_type="document", delegated_to="m-a"),
        _delegation("2", task_type="reasoning", delegated_to="m-b"),
        _delegation("3", task_type="reasoning", delegated_to="m-b"),
    )
    row = _read(path)[1]["rows"][0]
    assert row["byTaskType"] == [
        {"taskType": "reasoning", "count": 2},
        {"taskType": "document", "count": 1},
    ]
    assert row["byModel"][0] == {"model": "m-b", "count": 2}
    assert row["avgLatencyMs"] == pytest.approx(900.0)


def test_a_store_created_before_the_step_gains_the_view_on_its_next_open(
    tmp_path: Path,
) -> None:
    path = _store(tmp_path, _delegation("old"))
    conn = sqlite3.connect(path)
    conn.execute("DROP VIEW projection_delegation_summary")
    conn.execute(
        "DELETE FROM omnimarket_sqlite_store_steps "
        "WHERE step = 'omn20709_delegation_summary_view'"
    )
    conn.commit()
    conn.close()
    assert _read(path)[0] == 503

    SqliteDatabaseAdapter(path)._connect().close()

    status, body = _read(path)
    assert status == 200, body
    assert body["rows"][0]["totalDelegations"] == 1
