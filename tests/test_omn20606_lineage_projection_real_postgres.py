# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20606: the row names the delegation a fallback or escalation follows.

Real Postgres twin of ``tests/unit/projection/test_delegation_lineage_omn20606.py``:
the disposable schema is migrated with this node's full migration set (so
migration 0055 is applied), delegate-skill terminals are projected through the
REAL deployed writer, and the lineage columns are read back, including the join
from a fallback row to the failed row it follows.

The database is the ``INTEGRATION_POSTGRES_*`` one when its password is set
(CI), and otherwise a disposable native cluster this module starts itself
(``local_postgres``). A host with neither FAILS rather than skips: a skipped
real-database proof exits 0 although it proved nothing.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta
from tests.test_omn15359_ac3_replay_real_postgres import (
    local_postgres as _native_cluster,
)
from tests.test_omn15909_real_postgres_projection_write_path_gate import (
    _MIGRATIONS_DIR,
    _provisioned_runner,
)
from tests.test_omn19514_ticket_id_projection_real_postgres import _ROLES_SQL

_SELECT = (
    "SELECT parent_correlation_id, lineage_kind, parent_failure_cause, caller_lane "
    "FROM delegation_events WHERE correlation_id = $1"
)
_JOIN = (
    "SELECT child.correlation_id AS child, parent.terminal_failure_cause AS cause, "
    "child.lineage_kind AS kind FROM delegation_events child "
    "JOIN delegation_events parent "
    "ON parent.correlation_id::text = child.parent_correlation_id "
    "WHERE parent.correlation_id = $1"
)
_MIGRATION_0055 = _MIGRATIONS_DIR / "0055_delegation_events_lineage.sql"
_LANE = "deleg-fallback-mark-9143"

# pytest finds a fixture by module attribute; the dsn fixture below requests it
# by this name only when no INTEGRATION_POSTGRES_* database is configured.
local_postgres = _native_cluster


@pytest.fixture
async def dsn(request: pytest.FixtureRequest) -> str | None:
    """None selects the INTEGRATION_POSTGRES_* database; else a local cluster."""
    if os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    ):
        return None
    try:
        pg = request.getfixturevalue("local_postgres")[0]
    except pytest.skip.Exception as exc:
        pytest.fail(f"a skip is not a pass: no real Postgres to prove on ({exc})")
    url = f"postgresql://{pg.user}@/{pg.database}?host={pg.host}&port={pg.port}"
    admin = await asyncpg.connect(url)
    try:
        await admin.execute(_ROLES_SQL)
        await admin.execute("CREATE SCHEMA IF NOT EXISTS omninode_internal")
    finally:
        await admin.close()
    return url


@asynccontextmanager
async def _runner(
    dsn: str | None,
) -> AsyncIterator[tuple[DelegationProjectionRunner, asyncpg.Connection, str]]:
    """``_provisioned_runner``, with its skip on an unreachable database a failure."""
    try:
        async with _provisioned_runner(dsn) as provisioned:
            yield provisioned
    except pytest.skip.Exception as exc:
        pytest.fail(f"a skip is not a pass: no real Postgres to prove on ({exc})")


def _payload(correlation_id: str, **extra: object) -> dict[str, Any]:
    return {
        "status": "completed",
        "correlation_id": correlation_id,
        "task_type": "document",
        "tenant_id": "omninode",
        "metrics": {"cost_usd": 0.0},
        **extra,
    }


@pytest.mark.integration
class TestTheRowNamesItsLineage:
    async def test_a_fallback_row_joins_to_its_failed_parent(
        self, dsn: str | None
    ) -> None:
        parent, child = str(uuid4()), str(uuid4())
        async with _runner(dsn) as (runner, admin_conn, _schema):
            assert await runner._project_delegate_skill_terminal(
                _payload(
                    parent,
                    status="failed",
                    quality_gate_passed=False,
                    terminal_failure_cause="provider_quota_exhausted",
                    caller_lane=_LANE,
                ),
                MessageMeta(partition=0, offset=1, fallback_id=parent),
            )
            assert await runner._project_delegate_skill_terminal(
                _payload(
                    child,
                    caller_lane=_LANE,
                    parent_correlation_id=parent,
                    lineage_kind="fallback",
                    parent_failure_cause="provider_quota_exhausted",
                ),
                MessageMeta(partition=0, offset=2, fallback_id=child),
            )
            row = await admin_conn.fetchrow(_SELECT, child)
            assert row is not None
            assert dict(row) == {
                "parent_correlation_id": parent,
                "lineage_kind": "fallback",
                "parent_failure_cause": "provider_quota_exhausted",
                "caller_lane": _LANE,
            }
            joined = await admin_conn.fetch(_JOIN, parent)
            assert [(str(r["child"]), r["kind"]) for r in joined] == [
                (child, "fallback")
            ]

    async def test_a_lineage_less_reemit_keeps_and_a_malformed_one_writes_none(
        self, dsn: str | None
    ) -> None:
        kept, malformed = str(uuid4()), str(uuid4())
        parent = str(uuid4())
        async with _runner(dsn) as (runner, admin_conn, _schema):
            meta = MessageMeta(partition=0, offset=3, fallback_id=kept)
            assert await runner._project_delegate_skill_terminal(
                _payload(kept, parent_correlation_id=parent, lineage_kind="escalation"),
                meta,
            )
            await runner._project_delegate_skill_terminal(_payload(kept), meta)
            row = await admin_conn.fetchrow(_SELECT, kept)
            assert row is not None
            assert (row["parent_correlation_id"], row["lineage_kind"]) == (
                parent,
                "escalation",
            )
            assert await runner._project_delegate_skill_terminal(
                _payload(malformed, lineage_kind="fallback"),
                MessageMeta(partition=0, offset=4, fallback_id=malformed),
            )
            row = await admin_conn.fetchrow(_SELECT, malformed)
            assert row is not None
            assert row["parent_correlation_id"] is None
            assert row["lineage_kind"] is None

    async def test_the_migration_adds_nullable_columns_and_the_parent_index(
        self, dsn: str | None
    ) -> None:
        async with _runner(dsn) as (_projection_runner, admin_conn, _schema):
            # Re-applying is a no-op: every statement is IF NOT EXISTS.
            await admin_conn.execute(_MIGRATION_0055.read_text(encoding="utf-8"))
            columns = await admin_conn.fetch(
                "SELECT column_name, is_nullable, column_default "
                "FROM information_schema.columns WHERE table_name = 'delegation_events' "
                "AND column_name = ANY($1::text[])",
                ["parent_correlation_id", "lineage_kind", "parent_failure_cause"],
            )
            assert sorted((r["column_name"], r["is_nullable"]) for r in columns) == [
                ("lineage_kind", "YES"),
                ("parent_correlation_id", "YES"),
                ("parent_failure_cause", "YES"),
            ]
            assert all(r["column_default"] is None for r in columns)
            index = await admin_conn.fetchval(
                "SELECT indexdef FROM pg_indexes "
                "WHERE indexname = 'idx_delegation_events_parent_correlation_id'"
            )
            assert index is not None
            assert "WHERE (parent_correlation_id IS NOT NULL)" in index
