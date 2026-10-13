# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19987: real-Postgres proof of the run-trace writer SQL and its migration.

The writer's insert has no Python branch, and the table's key, checks and JSONB
column exist only in the migration, so only a real database proves them. SKIPS
without a reachable database and uses its own throwaway schema, like the
usage-by-model-day proof.

Failure modes each test is written against:

* the migration does not apply, or does not apply twice;
* a chain delivered shuffled, with equal timestamps, reads back in another order
  than the step sequence;
* a replayed step is inserted again, rewritten, or given a new cursor;
* another tenant's step with the same run id and sequence collides with, or
  overwrites, this tenant's;
* the candidates do not survive the JSONB round trip in order;
* the database accepts a step the model would refuse (sequence 0, an unknown
  kind, a terminal naming itself as the failing step).
"""

from __future__ import annotations

import json
import os
import random
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import asyncpg
import pytest

from omnimarket.models.model_delegation_run_step import ModelDelegationRunStep
from omnimarket.nodes.node_projection_run_trace.handlers.handler_projection_run_trace import (
    HandlerProjectionRunTrace,
)
from omnimarket.nodes.node_projection_run_trace.handlers.handler_run_trace_writer import (
    _INSERT_STEP,
    _SELECT_STORED_STEP,
)

_ROOT = Path(__file__).resolve().parents[1]
_MIGRATIONS_DIR = _ROOT / "src/omnimarket/nodes/node_projection_run_trace/migrations"
# The create and the FORCE step. 0001 grants a cluster role its privileges and
# is proven on the lab against a database that has that role.
_MIGRATIONS = (
    _MIGRATIONS_DIR / "0000_create_run_trace.sql",
    _MIGRATIONS_DIR / "0002_force_rls_run_trace.sql",
)
_CHAIN = _ROOT / "tests/fixtures/omn19987/run_step_chain.json"
_SCHEMA = "omn19987_run_trace_test"
_OTHER_TENANT = "11111111-2222-4333-8444-555555555555"


async def _connect_or_skip() -> asyncpg.Connection:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip("INTEGRATION_POSTGRES_PASSWORD not set -- skipping run-trace proof")
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    dsn = f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"
    conn: asyncpg.Connection | None = None
    try:
        conn = await asyncpg.connect(dsn)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no reachable Postgres for run-trace write-path proof: {exc}")
    assert conn is not None
    return conn


def _scoped(statement: str, schema: str) -> str:
    # The dashboard reader role is cluster-scoped and absent from a throwaway
    # database; the grant is kept, aimed at the connecting role.
    return statement.replace("public.", f"{schema}.").replace(
        "TO app_dashboard", "TO CURRENT_USER"
    )


async def _fresh(conn: asyncpg.Connection, schema: str) -> None:
    await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    await conn.execute(f"CREATE SCHEMA {schema}")
    # Twice: each migration is re-runnable over its own result.
    for _ in range(2):
        for migration in _MIGRATIONS:
            await conn.execute(_scoped(migration.read_text(encoding="utf-8"), schema))


async def _insert(
    conn: asyncpg.Connection, schema: str, event: dict[str, Any]
) -> list[asyncpg.Record]:
    step = HandlerProjectionRunTrace().handle(
        ModelDelegationRunStep.model_validate(event)
    )
    inserted: list[asyncpg.Record] = await conn.fetch(
        _scoped(_INSERT_STEP, schema),
        step.tenant_id,
        step.correlation_id,
        step.step_sequence,
        step.step_kind.value,
        step.started_at,
        step.duration_ms,
        step.outcome.value,
        step.failure_cause,
        step.backend_id,
        step.model_id,
        step.attempt_number,
        step.reason,
        json.dumps(list(step.candidates)),
        step.failing_step_sequence,
    )
    return inserted


def _chain() -> list[dict[str, Any]]:
    chain: list[dict[str, Any]] = json.loads(_CHAIN.read_text(encoding="utf-8"))
    return chain


@pytest.mark.integration
async def test_real_postgres_a_redelivered_step_reads_back_the_stored_row_to_publish_again() -> (
    None
):
    """The read the writer makes when a redelivery conflicts on the step's key.

    A publish that failed after the insert committed is repaired by publishing
    the stored row when the event comes back, so that read has to return the
    row exactly as the insert returned it, whatever the redelivery carries.
    """
    conn = await _connect_or_skip()
    try:
        await _fresh(conn, _SCHEMA)
        event = _chain()[1]
        first = await _insert(conn, _SCHEMA, event)
        assert len(first) == 1
        changed = {**event, "reason": "rewritten", "backend_id": "another-backend"}
        assert await _insert(conn, _SCHEMA, changed) == []

        step = HandlerProjectionRunTrace().handle(
            ModelDelegationRunStep.model_validate(changed)
        )
        select = _scoped(_SELECT_STORED_STEP, _SCHEMA)
        stored = await conn.fetch(
            select, step.tenant_id, step.correlation_id, step.step_sequence
        )
        assert len(stored) == 1
        assert dict(stored[0]) == dict(first[0])
        assert stored[0]["reason"] == event["reason"]
        assert stored[0]["backend_id"] == event["backend_id"]

        never_stored = await conn.fetch(
            select, step.tenant_id, step.correlation_id, step.step_sequence + 900
        )
        assert never_stored == []
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()


@pytest.mark.integration
async def test_real_postgres_shuffled_chain_reads_back_in_step_order_and_replay_is_a_no_op() -> (
    None
):
    conn = await _connect_or_skip()
    try:
        await _fresh(conn, _SCHEMA)
        chain = _chain()
        tenant = chain[0]["tenant_id"]
        chain[2]["started_at"] = chain[1]["started_at"]
        chain[-1]["started_at"] = chain[0]["started_at"]
        foreign = {**chain[3], "tenant_id": _OTHER_TENANT, "reason": "foreign"}
        delivered = [*chain, foreign]
        random.Random(7).shuffle(delivered)
        assert [e for e in delivered if e is not foreign] != chain
        for event in delivered:
            assert len(await _insert(conn, _SCHEMA, event)) == 1

        query = (
            f"SELECT * FROM {_SCHEMA}.run_trace WHERE tenant_id = $1 "
            "ORDER BY correlation_id ASC, step_sequence ASC"
        )
        rows = await conn.fetch(query, tenant)
        assert [r["step_sequence"] for r in rows] == [e["step_sequence"] for e in chain]
        assert [r["step_kind"] for r in rows] == [e["step_kind"] for e in chain]
        assert "foreign" not in {r["reason"] for r in rows}
        theirs = await conn.fetch(query, _OTHER_TENANT)
        assert [(r["tenant_id"], r["reason"]) for r in theirs] == [
            (_OTHER_TENANT, "foreign")
        ]
        routing = next(e for e in chain if e["step_kind"] == "routing_decision")
        stored = next(r for r in rows if r["step_sequence"] == routing["step_sequence"])
        assert json.loads(stored["candidates"]) == routing["candidates"]

        # Replay, reversed, with one step altered: nothing is inserted or rewritten.
        altered = {**chain[1], "outcome": "failed", "reason": "rewritten"}
        for event in [*reversed(delivered), altered]:
            assert await _insert(conn, _SCHEMA, event) == []
        again = await conn.fetch(query, tenant)
        assert [dict(r) for r in again] == [dict(r) for r in rows]
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()


@pytest.mark.integration
async def test_real_postgres_the_migration_declares_each_constraint_once() -> None:
    conn = await _connect_or_skip()
    schema = f"{_SCHEMA}_constraints"
    try:
        await _fresh(conn, schema)
        names = [
            r["conname"]
            for r in await conn.fetch(
                "SELECT conname FROM pg_constraint WHERE conrelid = $1::regclass "
                "ORDER BY conname",
                f"{schema}.run_trace",
            )
        ]
        security = await conn.fetchrow(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE oid = $1::regclass",
            f"{schema}.run_trace",
        )
        assert security is not None
        assert (security["relrowsecurity"], security["relforcerowsecurity"]) == (
            True,
            True,
        )
        policies = await conn.fetch(
            "SELECT polname FROM pg_policy WHERE polrelid = $1::regclass",
            f"{schema}.run_trace",
        )
        assert [r["polname"] for r in policies] == ["tenant_isolation"]
        assert names == [
            "run_trace_attempt_number_check",
            "run_trace_duration_ms_check",
            "run_trace_failing_step_sequence_check",
            "run_trace_outcome_check",
            "run_trace_pkey",
            "run_trace_step_kind_check",
            "run_trace_step_sequence_check",
        ]
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    "override",
    [
        {"step_sequence": 0},
        {"step_kind": "thinking"},
        {"outcome": "maybe"},
        {"duration_ms": -1},
        {"attempt_number": 0},
        {"failing_step_sequence": 2},
        {"tenant_id": None},
        {"correlation_id": None},
    ],
)
async def test_real_postgres_the_table_refuses_what_the_model_refuses(
    override: dict[str, Any],
) -> None:
    """Written straight to the table, past the model: the constraints hold alone."""
    conn = await _connect_or_skip()
    schema = f"{_SCHEMA}_checks"
    try:
        await _fresh(conn, schema)
        row: dict[str, Any] = {
            "tenant_id": "t",
            "correlation_id": "5b0b6c1e-3f52-4f0e-9a44-2f1f5d6f7a10",
            "step_sequence": 2,
            "step_kind": "terminal",
            "started_at": datetime(2026, 10, 11, 10, tzinfo=UTC),
            "duration_ms": 1,
            "outcome": "failed",
            "attempt_number": 1,
            "failing_step_sequence": 1,
        }
        columns = ", ".join(row)
        insert = (
            f"INSERT INTO {schema}.run_trace ({columns}) VALUES "
            "($1, $2, $3, $4, $5, $6, $7, $8, $9)"
        )
        # Positive control: the unaltered row is accepted, so a refusal below is
        # the constraint under test and not a malformed statement.
        await conn.execute(insert, *row.values())
        await conn.execute(f"DELETE FROM {schema}.run_trace")
        with pytest.raises(
            (
                asyncpg.CheckViolationError,
                asyncpg.NotNullViolationError,
            )
        ):
            await conn.execute(insert, *{**row, **override}.values())
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()
