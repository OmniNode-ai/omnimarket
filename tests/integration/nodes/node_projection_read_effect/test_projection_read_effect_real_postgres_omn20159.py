# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The projection read effect node against a real, disposable PostgreSQL (OMN-20159).

The unit suite proves the node's refusals over a fake row source. What only a
real database can prove:

* AC2 -- the node reaches the materialized table through the runtime binding
  overlay (the same ``ModelProjectionRuntimeBinding`` the projection runners
  use), with no DSN handed to it and none read by it.
* AC3 -- the WHERE clause actually scopes rows to the tenant: two tenants'
  rows sit in one table and each read returns only its own.
* AC5 -- a declared exposure whose table was never created answers a named
  refusal from the database, never ``ok`` with zero rows.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import yaml

from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.models import (
    ModelProjectionReadRequest,
)
from omnimarket.projection.discovery import parse_order_by_clauses
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.table_reader import TableRowSource
from tests.test_omn15359_ac3_replay_real_postgres import local_postgres  # noqa: F401

_DECISIONS = "onex.snapshot.projection.delegation.decisions.v1"
_MISSING = "onex.snapshot.projection.never-written.v1"
_TENANT_A = "820272f9-4aaf-5add-a2df-0af942852ab2"
_TENANT_B = "11111111-2222-4333-8444-555555555555"
_SCHEMA = "omn20159"
_OVERLAY_ENV = "OMNIMARKET_PROJECTION_RUNTIME_BINDING_OVERLAY"


def _cfg(topic: str, table: str) -> ProjectionTableConfig:
    columns = ("correlation_id", "tenant_id", "written_at", "cost_usd")
    return ProjectionTableConfig(
        topic=topic,
        table=table,
        schema_name=_SCHEMA,
        relation_schema=_SCHEMA,
        columns=columns,
        order_by="written_at DESC",
        order_by_spec=parse_order_by_clauses("written_at DESC", columns),
        freshness_column="written_at",
        limit=500,
        bus_backed=True,
        key_columns=("correlation_id",),
        tenant_column="tenant_id",
    )


def _topic_map() -> dict[str, ProjectionTableConfig]:
    return {
        _DECISIONS: _cfg(_DECISIONS, "delegation_events"),
        _MISSING: _cfg(_MISSING, "never_written"),
    }


@pytest.fixture
def dsn(request: pytest.FixtureRequest) -> str:
    """CI's INTEGRATION_POSTGRES_* database when set, else a disposable local one."""
    password = os.environ.get("INTEGRATION_POSTGRES_PASSWORD")
    if password:
        host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
        port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
        database = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
        user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
        return f"postgresql://{user}:{password}@{host}:{port}/{database}"
    pg = request.getfixturevalue("local_postgres")[0]
    return f"postgresql://postgres@/{pg.database}?host={pg.host}"


@pytest.fixture
async def seeded(dsn: str) -> AsyncIterator[None]:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.execute(f"CREATE SCHEMA {_SCHEMA}")
        await conn.execute(
            f"CREATE TABLE {_SCHEMA}.delegation_events ("
            " correlation_id uuid PRIMARY KEY, tenant_id text NOT NULL,"
            " written_at timestamptz NOT NULL, cost_usd numeric)"
        )
        rows = [(_TENANT_A, i) for i in range(3)] + [(_TENANT_B, i) for i in range(2)]
        for n, (tenant, i) in enumerate(rows):
            await conn.execute(
                f"INSERT INTO {_SCHEMA}.delegation_events VALUES"
                " ($1::uuid, $2, $3, 0.01)",
                f"20159000-0000-4000-8000-{n:012d}",
                tenant,
                datetime(2026, 9, 30, 12, i, tzinfo=UTC),
            )
        yield
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()


@pytest.fixture
def bound(dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The runtime's binding overlay names the database; no DSN env is set."""
    overlay = tmp_path / "projection-binding.yaml"
    overlay.write_text(
        yaml.safe_dump({"kafka_bootstrap_servers": "127.0.0.1:1", "database_url": dsn})
    )
    monkeypatch.setenv(_OVERLAY_ENV, str(overlay))
    monkeypatch.delenv("OMNIDASH_ANALYTICS_DB_URL", raising=False)
    monkeypatch.delenv("OMNINODE_INTERNAL_DB_URL", raising=False)


async def _read(**request: Any) -> Any:
    handler = HandlerProjectionRead(topic_map=_topic_map())
    try:
        return await handler.handle(ModelProjectionReadRequest(**request))
    finally:
        await handler.close()


@pytest.mark.integration
async def test_reads_the_table_through_the_runtime_binding(
    seeded: None, bound: None
) -> None:
    result = await _read(topic=_DECISIONS, tenant_id=_TENANT_A)
    assert result.ok is True, result
    assert result.row_count == 3
    assert result.response["backing"] == "table"


@pytest.mark.integration
async def test_tenant_rows_are_scoped_to_the_request_tenant(
    seeded: None, bound: None
) -> None:
    a = await _read(topic=_DECISIONS, tenant_id=_TENANT_A)
    b = await _read(topic=_DECISIONS, tenant_id=_TENANT_B)
    assert {row["tenant_id"] for row in a.rows} == {_TENANT_A}
    assert {row["tenant_id"] for row in b.rows} == {_TENANT_B}
    assert (a.row_count, b.row_count) == (3, 2)


@pytest.mark.integration
async def test_tenant_missing_on_scoped_exposure_reads_nothing(
    seeded: None, bound: None
) -> None:
    result = await _read(topic=_DECISIONS)
    assert result.ok is False
    assert result.error == "tenant_context_unresolved"
    assert result.rows == []


@pytest.mark.integration
async def test_unmaterialized_table_is_a_named_refusal(
    seeded: None, bound: None
) -> None:
    result = await _read(topic=_MISSING, tenant_id=_TENANT_A)
    assert result.ok is False
    assert result.error == "projection_table_missing"
    assert result.rows == []


@pytest.mark.integration
async def test_postgres_binding_is_unchanged_by_sqlite_support(
    seeded: None, bound: None
) -> None:
    """OMN-20329: a Postgres binding still reads, and close() still drains its pools."""
    handler = HandlerProjectionRead(topic_map=_topic_map())
    result = await handler.handle(
        ModelProjectionReadRequest(topic=_DECISIONS, tenant_id=_TENANT_A)
    )
    source = handler._owned_source
    assert result.ok is True, result
    assert isinstance(source, TableRowSource)
    assert source._pools, "the read opened a pool on the bound database"
    await handler.close()
    assert source._pools == {}
    assert handler._owned_source is None
