# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-26623 on real Postgres; failure modes Q1 the Postgres cells differ from
the local store's cells for the same recorded rows (a key, a value, a null or
the order); Q2 a figure differs from a direct count; Q3 a week with no runs
serves a rate instead of null; Q4 another tenant's runs are counted; Q5 before
0070 there is no relation (the mechanical RED half). WHICH DATABASE: the
OMN-21970 module's postgres fixture: CI's PostgreSQL 16 when
INTEGRATION_POSTGRES_PASSWORD is set, otherwise an owned localhost-only
container; skipped when docker is absent, so a skip proves nothing.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml

from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.models import (
    ModelProjectionReadRequest,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.discovery import (
    build_projection_topic_map,
    load_projection_exposures_from_contract,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from omnimarket.projection.table_reader import TableRowSource
from tests.projection.test_omn26623_delegation_quality_weekly import (
    _direct_count,
    _recorded,
)
from tests.test_omn21970_daily_and_cursor_real_postgres import (
    _Postgres,
    _provisioned,
)
from tests.test_omn21970_daily_and_cursor_real_postgres import postgres as postgres

pytestmark = pytest.mark.integration

_WEEKLY = "onex.snapshot.projection.delegation.quality-weekly.v1"
_QUALITY_MIGRATION = "0070_delegation_quality_weekly_view.sql"
_TENANT = UUID("0af28cd0-0654-42e0-a050-358ee8240940")
_OTHER = UUID("11111111-2222-4333-8444-555555555555")
_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_delegation/contract.yaml"
)


async def _insert(
    admin: Any,
    row: dict[str, Any],
    tenant: UUID = _TENANT,
    suffix: str = "",
) -> None:
    first = (
        row["attempt_history"][0]["model_id"] if row["attempt_history"] else "unknown"
    )
    model = first or "unknown"
    at = datetime.fromisoformat(row["created_at"])
    await admin.execute(
        "INSERT INTO delegation_events "
        "(correlation_id, tenant_id, task_type, delegated_to, model_name, "
        "terminal_ok, terminal_failure_cause, data_source, attempt_history, "
        "created_at, written_at) "
        "VALUES ($1, $2, $3, $4, $4, $5, $6, $7, $8::jsonb, $9, $9)",
        row["correlation_id"] + suffix,
        tenant,
        row["task_type"],
        model,
        None if row["terminal_ok"] is None else bool(row["terminal_ok"]),
        row["terminal_failure_cause"],
        row["data_source"],
        json.dumps(row["attempt_history"]),
        at,
    )


def _topics(schema: str) -> dict[str, Any]:
    return {
        topic: cfg.model_copy(update={"relation_schema": schema})
        for topic, cfg in build_projection_topic_map().items()
        if topic == _WEEKLY
    }


async def _postgres_cells(
    pg: _Postgres,
    schema: str,
    tenant: UUID = _TENANT,
) -> list[dict[str, Any]]:
    source = TableRowSource.for_database_url(pg.dsn(schema))
    try:
        result = await HandlerProjectionRead(
            topic_map=_topics(schema), row_source=source
        ).handle(ModelProjectionReadRequest(topic=_WEEKLY, tenant_id=str(tenant)))
    finally:
        await source.close()
    assert result.ok, result.response
    rows = result.rows
    if not rows:
        return []
    assert len(rows) == 1
    cells = rows[0]["cells"]
    return json.loads(cells) if isinstance(cells, str) else list(cells)


def _sqlite_cells(tmp_path: Path, rows: Any) -> list[dict[str, Any]]:
    path = tmp_path / "delegation.sqlite"
    adapter = SqliteDatabaseAdapter(path)
    for row in rows:
        adapter.upsert(
            "delegation_events",
            "correlation_id",
            {
                **row,
                "tenant_id": str(_TENANT),
                "attempt_history": json.dumps(row["attempt_history"]),
            },
        )
    topics = {
        cfg.topic: cfg
        for cfg in load_projection_exposures_from_contract(
            yaml.safe_load(_CONTRACT.read_text(encoding="utf-8")),
            "node_projection_delegation",
            _CONTRACT,
        )
    }
    result = asyncio.run(
        HandlerProjectionRead(
            topic_map=topics,
            row_source=SqliteTableRowSource(path),
        ).handle(
            ModelProjectionReadRequest(
                topic=_WEEKLY,
                tenant_id=str(_TENANT),
                limit=500,
            )
        )
    )
    assert result.http_status == 200, result.response
    (row,) = result.response["rows"]
    cells = row["cells"]
    return json.loads(cells) if isinstance(cells, str) else list(cells)


@pytest.mark.integration
def test_q1_postgres_serves_the_local_stores_cells_for_the_recorded_rows(
    postgres: _Postgres, tmp_path: Path
) -> None:
    rows = _recorded()
    expected = _sqlite_cells(tmp_path, rows)

    async def body() -> list[dict[str, Any]]:
        async with _provisioned(postgres) as (admin, schema):
            for row in rows:
                await _insert(admin, row)
            return await _postgres_cells(postgres, schema)

    served = asyncio.run(body())
    assert len(served) == len(expected)
    # SQLite's JSON functions write a REAL with 15 significant digits,
    # so one rate reads 0.833333333333333 on SQLite and
    # 0.8333333333333334 on Postgres.
    for got, want in zip(served, expected, strict=True):
        assert set(got) == set(want)
        for key in got:
            if key in ("first_try_pass_rate", "moved_down_chain_rate"):
                if got[key] is None or want[key] is None:
                    assert got[key] is None
                    assert want[key] is None
                else:
                    assert got[key] == pytest.approx(want[key], rel=1e-12)
            else:
                assert got[key] == want[key]
    assert sum(c["runs"] for c in served) == sum(
        v[0] for v in _direct_count(rows).values()
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_q2_each_figure_equals_a_direct_count(
    postgres: _Postgres,
) -> None:
    rows = _recorded()
    async with _provisioned(postgres) as (admin, schema):
        for row in rows:
            await _insert(admin, row)
        cells = await _postgres_cells(postgres, schema)

    direct = _direct_count(rows)
    by_key = {
        (cell["iso_week"], cell["task_type"], cell["model_id"]): cell for cell in cells
    }
    for (week, task_type, model), (runs, passes, moved) in direct.items():
        cell = by_key[(week, task_type, model)]
        assert cell["runs"] == runs
        assert cell["first_try_passes"] == passes
        assert cell["moved_down_chain"] == moved
        assert cell["first_try_pass_rate"] == pytest.approx(passes / runs)
        assert cell["moved_down_chain_rate"] == pytest.approx(moved / runs)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_q3_a_week_with_no_runs_serves_no_rate(postgres: _Postgres) -> None:
    rows = _recorded()
    async with _provisioned(postgres) as (admin, schema):
        for row in rows:
            await _insert(admin, row)
        cells = await _postgres_cells(postgres, schema)

    assert any(cell["runs"] == 0 for cell in cells)
    for cell in cells:
        rate_keys = [k for k in cell if "rate" in k.lower()]
        if cell["runs"] == 0:
            assert all(cell[k] is None for k in rate_keys)
        else:
            assert all(cell[k] is not None for k in rate_keys)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_q4_another_tenants_runs_are_not_counted(postgres: _Postgres) -> None:
    rows = _recorded()
    async with _provisioned(postgres) as (admin, schema):
        for row in rows:
            await _insert(admin, row, _TENANT)
        other_row = next(row for row in rows if row["attempt_history"])
        await _insert(admin, other_row, _OTHER, suffix="-other")
        tenant_cells = await _postgres_cells(postgres, schema, _TENANT)
        other_cells = await _postgres_cells(postgres, schema, _OTHER)

    assert sum(c["runs"] for c in tenant_cells) == sum(
        v[0] for v in _direct_count(rows).values()
    )
    assert sum(c["runs"] for c in other_cells) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_q5_before_0070_there_is_no_relation(postgres: _Postgres) -> None:
    migration = _CONTRACT.parent / "migrations" / _QUALITY_MIGRATION
    async with _provisioned(postgres, stop_before=_QUALITY_MIGRATION) as (
        admin,
        schema,
    ):
        exists = await admin.fetchval(
            "SELECT to_regclass($1)",
            f"{schema}.projection_delegation_quality_weekly",
        )
        assert exists is None
        sql = migration.read_text(encoding="utf-8").replace("public.", f"{schema}.")
        await admin.execute(sql)
        exists = await admin.fetchval(
            "SELECT to_regclass($1)",
            f"{schema}.projection_delegation_quality_weekly",
        )
        assert exists is not None
