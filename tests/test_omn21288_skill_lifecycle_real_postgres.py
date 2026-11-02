# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The skill lifecycle writer's statement against real Postgres.

Applies the node's own migrations to a unique scratch schema, drives the
writer's actual INSERT through the pure fold, and reads the table back. Each
test runs in one transaction that is rolled back. Without INTEGRATION_POSTGRES
credentials this proof skips.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from uuid import UUID, uuid4

import asyncpg
import pytest
import yaml

from omnimarket.nodes.node_projection_skill_lifecycle.handlers.handler_projection_skill_lifecycle import (
    HandlerProjectionSkillLifecycle,
)
from omnimarket.nodes.node_projection_skill_lifecycle.handlers.handler_skill_lifecycle_writer import (
    _INSERT_SKILL_EXECUTION,
)
from omnimarket.nodes.node_projection_skill_lifecycle.models import (
    ModelSkillLifecycleProjectionRequest,
)

_NODE = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_skill_lifecycle"
)
_STARTED_TOPIC, _COMPLETED_TOPIC = yaml.safe_load(
    (_NODE / "contract.yaml").read_text()
)["event_bus"]["subscribe_topics"]
_TENANT = "omninode"
_RUN = "6f1d9d3e-3a53-4d3b-8f0b-0f1c1e6f1a01"
_CORRELATION = "0b7f1a52-7a5e-4e53-b1b1-5c0a6a9f2c02"


async def _connect_or_skip() -> asyncpg.Connection:
    dsn = os.environ.get("INTEGRATION_POSTGRES_DSN")
    if not dsn:
        secret = os.environ.get(
            "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
        )
        if not secret:
            pytest.skip(
                "INTEGRATION_POSTGRES_DSN/INTEGRATION_POSTGRES_PASSWORD unset -- "
                "skipping the skill lifecycle write-path proof"
            )
        host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
        port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
        user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
        db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
        dsn = f"postgresql://{quote_plus(user)}:{quote_plus(secret)}@{host}:{port}/{db}"
    try:
        return await asyncpg.connect(dsn, timeout=10)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no reachable Postgres for the write-path proof: {exc}")


def _scoped(statement: str, schema: str, role: str | None = None) -> str:
    """Retarget objects, and the writer role when given, to scratch names."""
    scoped = statement.replace("ON SCHEMA public", f"ON SCHEMA {schema}").replace(
        "public.", f"{schema}."
    )
    return scoped if role is None else scoped.replace("tenant_projection_writer", role)


def _started(**updates: Any) -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "run_id": _RUN,
        "skill_name": "ticket-pipeline",
        "skill_id": "skill-ticket-pipeline",
        "repo_id": "omnimarket",
        "correlation_id": _CORRELATION,
        "args_count": 2,
        "session_id": "session-1",
        "emitted_at": "2026-11-02T07:00:00+00:00",
    } | updates


def _completed(**updates: Any) -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "run_id": _RUN,
        "skill_name": "ticket-pipeline",
        "repo_id": "omnimarket",
        "correlation_id": _CORRELATION,
        "status": "partial",
        "duration_ms": 1250,
        "error_type": "Timeout",
        "started_emit_failed": True,
        "session_id": "session-1",
        "emitted_at": "2026-11-02T07:00:02+00:00",
    } | updates


async def _write(
    conn: asyncpg.Connection,
    schema: str,
    topic: str,
    payload: dict[str, Any],
    tenant: str = _TENANT,
) -> int:
    """Run the fold and the writer's own statement; return rows accepted."""
    row = (
        HandlerProjectionSkillLifecycle()
        .handle(ModelSkillLifecycleProjectionRequest(topic=topic, payload=payload))
        .row
    )
    assert row is not None
    returned = await conn.fetch(
        _scoped(_INSERT_SKILL_EXECUTION, schema),
        row.event_id,
        row.run_id,
        row.event_type.value,
        row.skill_name,
        row.skill_id,
        row.repo_id,
        row.correlation_id,
        row.args_count,
        row.status,
        row.duration_ms,
        row.error_type,
        row.started_emit_failed,
        row.session_id,
        row.emitted_at,
        tenant,
    )
    return len(returned)


async def _count(conn: asyncpg.Connection, schema: str) -> int:
    return int(await conn.fetchval(f"SELECT count(*) FROM {schema}.skill_executions"))


async def _with_scratch_schema(
    body: Callable[[asyncpg.Connection, str], Awaitable[None]],
) -> None:
    """Run ``body`` as a scratch non-superuser holding exactly the node's grants.

    Migration 0002 refuses a writer role that is a superuser or bypasses RLS, so
    the scratch role stands in for ``tenant_projection_writer`` and every write
    below runs under the grants and the enforced tenant policy that role gets.
    """
    conn = await _connect_or_skip()
    schema = f"omn21933_skill_lifecycle_{uuid4().hex}"
    role = f"omn21933_writer_{uuid4().hex[:12]}"
    transaction = conn.transaction()
    try:
        await transaction.start()
        try:
            await conn.execute(f"CREATE ROLE {role} NOLOGIN")
        except asyncpg.InsufficientPrivilegeError:
            pytest.skip("integration user cannot create the scratch writer role")
        await conn.execute(f"CREATE SCHEMA {schema}")
        for migration in sorted((_NODE / "migrations").glob("*.sql")):
            await conn.execute(
                _scoped(migration.read_text(encoding="utf-8"), schema, role)
            )
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", _TENANT)
        await conn.execute(f"SET LOCAL ROLE {role}")
        await body(conn, schema)
    finally:
        try:
            await transaction.rollback()
        finally:
            await conn.close()


@pytest.mark.integration
async def test_started_and_completed_rows_join_on_run_id() -> None:
    async def body(conn: asyncpg.Connection, schema: str) -> None:
        assert await _write(conn, schema, _STARTED_TOPIC, _started()) == 1
        assert await _write(conn, schema, _COMPLETED_TOPIC, _completed()) == 1
        rows = await conn.fetch(
            f"SELECT * FROM {schema}.skill_executions ORDER BY emitted_at"
        )
        assert [r["event_type"] for r in rows] == ["started", "completed"]
        assert {r["run_id"] for r in rows} == {UUID(_RUN)}
        started, completed = rows
        assert (started["skill_id"], started["args_count"]) == (
            "skill-ticket-pipeline",
            2,
        )
        assert started["status"] is None
        assert (
            completed["status"],
            completed["duration_ms"],
            completed["error_type"],
            completed["started_emit_failed"],
        ) == ("partial", 1250, "Timeout", True)
        assert {r["tenant_id"] for r in rows} == {_TENANT}
        assert all(r["received_at"] is not None for r in rows)

    await _with_scratch_schema(body)


@pytest.mark.integration
async def test_two_replays_write_one_row_per_event() -> None:
    async def body(conn: asyncpg.Connection, schema: str) -> None:
        stream = [
            (_STARTED_TOPIC, _started()),
            (_COMPLETED_TOPIC, _completed()),
            # Old-schema events carry no id: the derived key is what converges.
            (_STARTED_TOPIC, {k: v for k, v in _started().items() if k != "event_id"}),
            (
                _COMPLETED_TOPIC,
                {k: v for k, v in _completed().items() if k != "event_id"},
            ),
        ]
        first = [await _write(conn, schema, t, e) for t, e in stream]
        assert first == [1, 1, 1, 1]
        assert await _count(conn, schema) == 4
        for _ in range(2):
            replay = [await _write(conn, schema, t, e) for t, e in stream]
            assert replay == [0, 0, 0, 0]
            assert await _count(conn, schema) == 4

    await _with_scratch_schema(body)


@pytest.mark.integration
async def test_a_replay_never_rewrites_the_first_row() -> None:
    async def body(conn: asyncpg.Connection, schema: str) -> None:
        event = _completed()
        assert await _write(conn, schema, _COMPLETED_TOPIC, event) == 1
        again = event | {"status": "failed", "duration_ms": 9}
        assert await _write(conn, schema, _COMPLETED_TOPIC, again) == 0
        row = await conn.fetchrow(
            f"SELECT status, duration_ms FROM {schema}.skill_executions"
        )
        assert row is not None
        assert (row["status"], row["duration_ms"]) == ("partial", 1250)

    await _with_scratch_schema(body)


@pytest.mark.integration
async def test_tenant_isolation_holds_for_the_non_superuser_writer() -> None:
    """The migration's FORCE RLS refuses a row stamped for another tenant."""

    async def body(conn: asyncpg.Connection, schema: str) -> None:
        assert await _write(conn, schema, _STARTED_TOPIC, _started()) == 1
        with pytest.raises(asyncpg.PostgresError, match="row-level security"):
            async with conn.transaction():
                await _write(
                    conn, schema, _STARTED_TOPIC, _started(), tenant="other-tenant"
                )
        assert await _count(conn, schema) == 1

    await _with_scratch_schema(body)
