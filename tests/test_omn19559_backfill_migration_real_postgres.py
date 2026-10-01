# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20276: the backfill of rows OMN-19559 left self-contradicting.

Before omnimarket#3158 a delegation_events row could read terminal_ok=false
with a typed failure cause while operational_outcome='completed' and
content_verdict='usable'. Migration 0051 rewrites those historical rows with
the rule #3158 applies to new ones, keeps each row's prior values in an audit
table, and its rollback restores them.

Driven against a disposable schema migrated with this node's full migration
set on real Postgres, never a mock. The harness SKIPS without
``INTEGRATION_POSTGRES_PASSWORD`` and a reachable server.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.nodes.node_projection_delegation.models.model_terminal_precedence import (
    outcome_for_failure_cause,
)
from tests.test_omn15909_real_postgres_projection_write_path_gate import (
    _APP_DASHBOARD_ROLE_SQL,
    _MIGRATIONS_DIR,
    _connect_or_skip,
    _test_schema_safe_sql,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_BACKFILL = _MIGRATIONS_DIR / "0051_delegation_events_failed_outcome_backfill.sql"
_ROLLBACK = (
    _MIGRATIONS_DIR.parent
    / "rollback"
    / "0051_delegation_events_failed_outcome_backfill.sql"
)
_AUDIT = "delegation_events_outcome_backfill_omn20276"
_TENANT = "820272f9-4aaf-5add-a2df-0af942852ab2"

# Every cause the core vocabulary carries, plus one it does not (the mapping's
# fallback branch), each on a row that reads completed/usable.
_CAUSES = (
    "timeout",
    "runtime_shutdown",
    "provider_quota_exhausted",
    "quality_gate_refused",
    "provider_error",
    "auth_failed",
    "no_terminal",
    "something_new",
)

# (terminal_ok, cause, outcome, verdict): rows the backfill must NOT touch.
_UNTOUCHED = (
    (True, None, "completed", "usable"),  # a real success
    (None, "timeout", "completed", "usable"),  # terminal_ok unknown, never coerced
    (False, "  ", "completed", "usable"),  # blank cause
    (False, None, "completed", "usable"),  # no cause
    (False, "timeout", "timeout", "not_applicable"),  # already consistent
    (False, "timeout", "inference_failed", "usable"),  # not 'completed'
)

_SNAPSHOT = (
    "SELECT correlation_id, terminal_ok, terminal_failure_cause, "
    "operational_outcome, content_verdict, created_at "
    "FROM delegation_events ORDER BY correlation_id"
)


def _base_migrations() -> list[Path]:
    return [p for p in sorted(_MIGRATIONS_DIR.glob("*.sql")) if p != _BACKFILL]


@contextlib.asynccontextmanager
async def _migrated_schema() -> AsyncIterator[tuple[asyncpg.Connection, str]]:
    conn = await _connect_or_skip()
    schema = f"omn20276_{uuid4().hex[:16]}"
    try:
        await conn.execute(f"CREATE SCHEMA {schema}")
        await conn.execute(f"SET search_path TO {schema}, public")
        await conn.execute(_APP_DASHBOARD_ROLE_SQL)
        for path in _base_migrations():
            await conn.execute(_test_schema_safe_sql(path.read_text(encoding="utf-8")))
        yield conn, schema
    finally:
        with contextlib.suppress(Exception):
            await conn.execute("RESET ROLE")
            await conn.execute("SET search_path TO public")
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()


async def _insert(
    conn: asyncpg.Connection,
    cid: str,
    terminal_ok: bool | None,
    cause: str | None,
    outcome: str | None,
    verdict: str | None,
) -> None:
    await conn.execute(
        "INSERT INTO delegation_events (correlation_id, tenant_id, terminal_ok, "
        "terminal_failure_cause, operational_outcome, content_verdict) "
        "VALUES ($1, $2::uuid, $3, $4, $5, $6)",
        cid,
        _TENANT,
        terminal_ok,
        cause,
        outcome,
        verdict,
    )


async def _seed(conn: asyncpg.Connection) -> None:
    for cause in _CAUSES:
        await _insert(conn, f"bad-{cause}", False, cause, "completed", "usable")
    await _insert(conn, "bad-correct", False, "timeout", "completed", "correct")
    await _insert(conn, "bad-null-verdict", False, "timeout", "completed", None)
    for i, row in enumerate(_UNTOUCHED):
        await _insert(conn, f"ok-{i}", *row)


async def _apply(conn: asyncpg.Connection, path: Path) -> None:
    await conn.execute(path.read_text(encoding="utf-8"))


async def _snapshot(conn: asyncpg.Connection) -> list[tuple[object, ...]]:
    return [tuple(r) for r in await conn.fetch(_SNAPSHOT)]


async def _contradictory(conn: asyncpg.Connection) -> int:
    return int(
        await conn.fetchval(
            "SELECT count(*) FROM delegation_events WHERE terminal_ok IS FALSE "
            "AND btrim(coalesce(terminal_failure_cause, '')) <> '' "
            "AND operational_outcome = 'completed'"
        )
    )


async def test_backfill_maps_each_cause_like_the_projection() -> None:
    async with _migrated_schema() as (conn, _schema):
        await _seed(conn)
        assert await _contradictory(conn) == len(_CAUSES) + 2

        await _apply(conn, _BACKFILL)

        assert await _contradictory(conn) == 0
        for cause in _CAUSES:
            row = await conn.fetchrow(
                "SELECT operational_outcome, content_verdict FROM delegation_events "
                "WHERE correlation_id = $1",
                f"bad-{cause}",
            )
            assert (row["operational_outcome"], row["content_verdict"]) == (
                outcome_for_failure_cause(cause)
            ), cause
        correct = await conn.fetchrow(
            "SELECT operational_outcome, content_verdict FROM delegation_events "
            "WHERE correlation_id = 'bad-correct'"
        )
        assert tuple(correct) == ("timeout", "not_applicable")
        null_verdict = await conn.fetchrow(
            "SELECT operational_outcome, content_verdict FROM delegation_events "
            "WHERE correlation_id = 'bad-null-verdict'"
        )
        assert tuple(null_verdict) == ("timeout", None)
        for i, (ok, cause, outcome, verdict) in enumerate(_UNTOUCHED):
            row = await conn.fetchrow(
                "SELECT terminal_ok, terminal_failure_cause, operational_outcome, "
                "content_verdict FROM delegation_events WHERE correlation_id = $1",
                f"ok-{i}",
            )
            assert tuple(row) == (ok, cause, outcome, verdict), i
        audited = await conn.fetchval(f"SELECT count(*) FROM {_AUDIT}")
        assert audited == len(_CAUSES) + 2


async def test_backfill_is_idempotent() -> None:
    async with _migrated_schema() as (conn, _schema):
        await _seed(conn)
        await _apply(conn, _BACKFILL)
        once = await _snapshot(conn)
        audit_once = await conn.fetch(f"SELECT * FROM {_AUDIT} ORDER BY 1")

        await _apply(conn, _BACKFILL)

        assert await _snapshot(conn) == once
        assert await conn.fetch(f"SELECT * FROM {_AUDIT} ORDER BY 1") == audit_once


async def test_rollback_restores_prior_values_and_drops_the_audit() -> None:
    async with _migrated_schema() as (conn, _schema):
        await _seed(conn)
        before = await _snapshot(conn)
        await _apply(conn, _BACKFILL)
        assert await _snapshot(conn) != before

        await _apply(conn, _ROLLBACK)

        assert await _snapshot(conn) == before
        assert await conn.fetchval("SELECT to_regclass($1)", _AUDIT) is None
        # Forward again after the rollback: the same rewrite, so the pair cycles.
        await _apply(conn, _BACKFILL)
        assert await _contradictory(conn) == 0


async def test_rollback_leaves_a_row_written_again_since_the_backfill() -> None:
    async with _migrated_schema() as (conn, _schema):
        await _seed(conn)
        await _apply(conn, _BACKFILL)
        await conn.execute(
            "UPDATE delegation_events SET operational_outcome = 'provider_quota' "
            "WHERE correlation_id = 'bad-timeout'"
        )

        await _apply(conn, _ROLLBACK)

        rewritten = await conn.fetchval(
            "SELECT operational_outcome FROM delegation_events "
            "WHERE correlation_id = 'bad-timeout'"
        )
        assert rewritten == "provider_quota"
        restored = await conn.fetchval(
            "SELECT operational_outcome FROM delegation_events "
            "WHERE correlation_id = 'bad-runtime_shutdown'"
        )
        assert restored == "completed"


async def test_backfill_sees_rows_under_force_rls_as_the_owner() -> None:
    """Under FORCE RLS the owner is filtered too; the backfill must still see rows."""
    async with _migrated_schema() as (conn, schema):
        await _seed(conn)
        owner = f"omn20276_owner_{uuid4().hex[:8]}"
        await conn.execute(f"CREATE ROLE {owner} NOLOGIN NOSUPERUSER NOBYPASSRLS")
        try:
            await conn.execute(f"GRANT USAGE, CREATE ON SCHEMA {schema} TO {owner}")
            await conn.execute(f"ALTER TABLE delegation_events OWNER TO {owner}")
            await conn.execute(
                "ALTER TABLE delegation_events ENABLE ROW LEVEL SECURITY"
            )
            await conn.execute("ALTER TABLE delegation_events FORCE ROW LEVEL SECURITY")
            await conn.execute(f"SET ROLE {owner}")
            # Positive control: the owner is blind to every row under FORCE.
            assert await conn.fetchval("SELECT count(*) FROM delegation_events") == 0

            await _apply(conn, _BACKFILL)

            await conn.execute("RESET ROLE")
            assert await _contradictory(conn) == 0
            forced = await conn.fetchval(
                "SELECT relforcerowsecurity FROM pg_class "
                "WHERE oid = 'delegation_events'::regclass"
            )
            assert forced is True
        finally:
            await conn.execute("RESET ROLE")
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            await conn.execute(f"DROP ROLE IF EXISTS {owner}")
