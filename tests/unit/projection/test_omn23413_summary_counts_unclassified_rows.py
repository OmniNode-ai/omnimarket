# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local summary counts rows written before data_source existed (OMN-23413 AC51).

The OMN-23169 summary filtered ``data_source <> 'experiment'``. In SQL a NULL
compared with ``<>`` is NULL, never true, so every row with no data_source
dropped out of every total. On the operator's installed store (2026-11-17
10:08Z) that was 170 rows written by the older claude-code hook path from
2026-09-29 to 2026-11-13: the dashboard read 3876 delegations while
delegation_events held 4046.

Each test names the failure it exists to catch:

* a row with no data_source is left out of the headline totals;
* an installed store that already ran the OMN-23169 step keeps the old
  definition, because a recorded step never runs again;
* the fix lets experiment rows back into the totals (the positive control:
  OMN-23169's exclusion must still hold).

Reads go through the real read node over :class:`SqliteTableRowSource`, as
``onex dashboard`` does.
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
_OMN23169_STEP = "omn23169_delegation_summary_excludes_experiment"
_OMN23169_SQL = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_projection_delegation/sqlite"
    / "delegation_summary_view_v2.sql"
)

_TOPICS = build_projection_topic_map()


def _delegation(correlation_id: str, **fields: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "correlation_id": correlation_id,
        "tenant_id": _TENANT,
        "task_type": "reasoning",
        "delegated_to": "Beam-1.0-501B-A23B",
        "quality_gate_passed": True,
        "latency_ms": 900,
        "cost_savings_usd": 0.25,
        "data_source": "real",
        "created_at": "2026-11-17T10:02:18.827938+00:00",
    }
    row.update(fields)
    return row


def _store(tmp_path: Path, *rows: dict[str, Any]) -> Path:
    path = tmp_path / "delegation.sqlite"
    adapter = SqliteDatabaseAdapter(path)
    for row in rows:
        adapter.upsert("delegation_events", "correlation_id", row)
    return path


def _null_data_source(path: Path, correlation_id: str) -> None:
    """Make one row look like a pre-classification row, as the installed store has."""
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE delegation_events SET data_source = NULL WHERE correlation_id = ?",
        (correlation_id,),
    )
    conn.commit()
    (value,) = conn.execute(
        "SELECT data_source FROM delegation_events WHERE correlation_id = ?",
        (correlation_id,),
    ).fetchone()
    conn.close()
    assert value is None


def _summary(path: Path) -> dict[str, Any]:
    handler = HandlerProjectionRead(
        topic_map=_TOPICS, row_source=SqliteTableRowSource(path)
    )
    result = asyncio.run(
        handler.handle(ModelProjectionReadRequest(topic=_SUMMARY, tenant_id=_TENANT))
    )
    assert result.http_status == 200, result.response
    (row,) = dict(result.response or {})["rows"]
    return dict(row)


def test_a_row_with_no_data_source_is_counted(tmp_path: Path) -> None:
    path = _store(
        tmp_path,
        _delegation("classified"),
        _delegation("legacy", quality_gate_passed=False, cost_savings_usd=0.5),
    )
    _null_data_source(path, "legacy")

    row = _summary(path)

    assert row["totalDelegations"] == 2
    assert row["qualityGateTotal"] == 2
    assert row["qualityGatePassed"] == 1
    assert row["totalSavingsUsd"] == pytest.approx(0.75)
    assert {entry["model"]: entry["count"] for entry in row["byModel"]} == {
        "Beam-1.0-501B-A23B": 2
    }


def test_an_installed_store_on_the_omn23169_view_gains_the_fix_on_next_open(
    tmp_path: Path,
) -> None:
    path = _store(tmp_path, _delegation("classified"), _delegation("legacy"))
    _null_data_source(path, "legacy")
    # Put the store back where the operator's install is: the OMN-23169 view in
    # place, and no step recorded after it.
    conn = sqlite3.connect(path)
    conn.execute("DROP VIEW projection_delegation_summary")
    conn.execute(_OMN23169_SQL.read_text(encoding="utf-8"))
    conn.execute(
        "DELETE FROM omnimarket_sqlite_store_steps WHERE step > ?",
        (_OMN23169_STEP,),
    )
    conn.commit()
    conn.close()
    assert _summary(path)["totalDelegations"] == 1  # the bug, reproduced

    SqliteDatabaseAdapter(path)._connect().close()

    assert _summary(path)["totalDelegations"] == 2


def test_experiment_rows_stay_out_of_the_totals(tmp_path: Path) -> None:
    path = _store(
        tmp_path,
        _delegation("classified"),
        _delegation("legacy"),
        _delegation("synthetic", data_source="experiment", cost_savings_usd=9.0),
    )
    _null_data_source(path, "legacy")

    row = _summary(path)

    assert row["totalDelegations"] == 2
    assert row["totalSavingsUsd"] == pytest.approx(0.5)
