# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21326 AC3 / OMN-17427: real-Postgres write and read path for delegation_budget_readings.

The writer tests drive the statements against a recording double, which binds
any Python type and runs no SQL. The UUID key, the ON CONFLICT target and the
observed_at ordering guard exist only in SQL, and so does the readback's
window query, so a real database is the only place they can be proven. Mirrors
tests/test_omn19566_lab_proof_receipts_real_postgres.py: it SKIPS without a
reachable database and uses its own throwaway schema.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote_plus
from uuid import UUID

import asyncpg
import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator import runtime_budget_readback
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.budget_reading_source import (
    ProjectionBudgetReadingSource,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models import (
    ModelBudgetReadbackSource,
)
from omnimarket.nodes.node_projection_delegation_budget_readings.handlers.handler_delegation_budget_readings_writer import (
    _SELECT_PRIOR,
    _UPSERT,
    _bind,
    _prior_row,
    event_from_delivery,
)
from omnimarket.nodes.node_projection_delegation_budget_readings.handlers.handler_projection_delegation_budget_readings import (
    HandlerProjectionDelegationBudgetReadings,
)
from omnimarket.nodes.node_projection_delegation_budget_readings.models import (
    ModelDelegationBudgetReadingProjectionRequest,
    ModelDelegationBudgetReadingRow,
)
from tests.test_omn21326_delegation_budget_readings_projection import (
    CORRELATION_ID,
    T0,
    _delivery,
)

_NODE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_delegation_budget_readings"
)
_MIGRATION = _NODE / "migrations" / "0000_create_delegation_budget_readings.sql"
_SCHEMA = "omn21326_delegation_budget_readings_test"
_DSN_ENV = "OMN21326_READBACK_TEST_DSN"


def _dsn_or_skip() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD not set -- skipping delegation_budget_readings DB proof"
        )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


async def _connect_or_skip() -> tuple[asyncpg.Connection, str]:
    dsn = _dsn_or_skip()
    try:
        return await asyncpg.connect(dsn), dsn
    except (
        OSError,
        asyncpg.PostgresError,
    ) as exc:
        pytest.skip(
            f"no reachable Postgres for delegation_budget_readings proof: {exc}"
        )


def _scoped(statement: str) -> str:
    return statement.replace("omninode_internal.", f"{_SCHEMA}.")


def _row(
    correlation_id: UUID = CORRELATION_ID, **changes: object
) -> ModelDelegationBudgetReadingRow:
    event = event_from_delivery(_delivery(T0, correlation_id=correlation_id))
    result = HandlerProjectionDelegationBudgetReadings().handle(
        ModelDelegationBudgetReadingProjectionRequest(event=event)
    )
    return result.row.model_copy(update=changes)


async def _setup(conn: asyncpg.Connection) -> None:
    await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await conn.execute(f"CREATE SCHEMA {_SCHEMA}")
    await conn.execute(_scoped(_MIGRATION.read_text(encoding="utf-8")))


@pytest.mark.integration
async def test_real_postgres_stores_the_terminal_and_reads_it_back() -> None:
    conn, _ = await _connect_or_skip()
    try:
        await _setup(conn)
        row = _row()
        returned = await conn.fetch(_scoped(_UPSERT), *_bind(row))
        assert len(returned) == 1
        assert returned[0]["correlation_id"] == row.correlation_id
        assert isinstance(returned[0]["projection_cursor"], int)
        prior = await conn.fetch(_scoped(_SELECT_PRIOR), row.correlation_id)
        assert _prior_row(dict(prior[0])) == row
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()


@pytest.mark.integration
async def test_real_postgres_refuses_an_older_terminal_of_the_same_id() -> None:
    conn, _ = await _connect_or_skip()
    try:
        await _setup(conn)
        row = _row()
        assert await conn.fetch(_scoped(_UPSERT), *_bind(row))
        older = _row(
            observed_at=row.observed_at - timedelta(minutes=1),
            execution_duration_ms=1,
        )
        assert await conn.fetch(_scoped(_UPSERT), *_bind(older)) == []
        read = f"SELECT execution_duration_ms FROM {_SCHEMA}.delegation_budget_readings"
        assert await conn.fetchval(read) == row.execution_duration_ms
        # positive control: a newer terminal of the id replaces the row
        newer = _row(
            observed_at=row.observed_at + timedelta(minutes=1),
            execution_duration_ms=7,
        )
        assert await conn.fetch(_scoped(_UPSERT), *_bind(newer))
        assert await conn.fetchval(read) == 7
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()


@pytest.mark.integration
async def test_real_postgres_readback_reads_the_window_through_the_dsn_variable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The planted falsifier end to end: rows in, the readback's exit status out."""
    conn, dsn = await _connect_or_skip()
    try:
        await _setup(conn)
        inside = UUID("00000000-0000-4000-8000-0000000000a1")
        over = UUID("00000000-0000-4000-8000-0000000000a2")
        outside_window = UUID("00000000-0000-4000-8000-0000000000a3")
        failed = UUID("00000000-0000-4000-8000-0000000000a4")
        for run, duration, shift, status in (
            (inside, 65_000, 0, "completed"),
            (over, 65_001, 0, "completed"),
            (outside_window, 99_000, 48, "completed"),
            (failed, 99_000, 0, "failed"),
        ):
            row = _row(run, execution_duration_ms=duration, status=status)
            row = row.model_copy(
                update={"observed_at": T0 - timedelta(hours=1 + shift)}
            )
            assert await conn.fetch(_scoped(_UPSERT), *_bind(row))

        monkeypatch.setenv(_DSN_ENV, dsn)
        source = ProjectionBudgetReadingSource(
            ModelBudgetReadbackSource(
                relation=f"{_SCHEMA}.delegation_budget_readings",
                dsn_env=_DSN_ENV,
                binding_ref="test",
                populated_by="omnimarket.nodes.node_projection_delegation_budget_readings",
            )
        )
        code = runtime_budget_readback.main(
            ["--until", T0.isoformat()], projection=source
        )
        summary = json.loads(capsys.readouterr().out)

        assert code == 1
        assert summary["deployed_lane_completed"] == 2
        assert [run["run_id"] for run in summary["over_runs"]] == [str(over)]
        assert dsn not in json.dumps(summary)

        await conn.execute(
            f"DELETE FROM {_SCHEMA}.delegation_budget_readings WHERE correlation_id = $1",
            over,
        )
        code = runtime_budget_readback.main(
            ["--until", T0.isoformat()], projection=source
        )
        capsys.readouterr()
        assert code == 0
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()
