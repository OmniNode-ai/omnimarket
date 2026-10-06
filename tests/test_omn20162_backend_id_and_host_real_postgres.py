# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20162: the row stores the accepting attempt's backend_id and host.

Real Postgres twin of ``tests/unit/projection/test_delegation_backend_id_and_host_omn20162.py``:
the disposable schema is migrated with this node's full migration set (so
migration 0054 is applied), a canonical terminal is projected through the REAL
``project_event()``, and each column is read back.

The database is the ``INTEGRATION_POSTGRES_*`` one when its password is set
(CI), and otherwise a disposable native cluster this module starts itself
(``local_postgres``: initdb into a temporary directory, socket only, removed at
module end). A host with neither FAILS rather than skips: the done gate runs
this file as ``uv run pytest`` and reads only the exit code, and a skipped
real-database proof exits 0 there although it proved nothing.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

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
from tests.test_omn18928_terminal_outcome_projection_real_postgres import (
    _quota_terminal,
    _wire,
)
from tests.test_omn19514_ticket_id_projection_real_postgres import _ROLES_SQL

_SELECT = (
    "SELECT backend_id, host, answering_backend FROM delegation_events "
    "WHERE correlation_id = $1"
)
_MIGRATION_0054 = _MIGRATIONS_DIR / "0054_delegation_events_backend_id_and_host.sql"

# pytest finds a fixture by module attribute; the dsn fixture below requests it
# by this name only when no INTEGRATION_POSTGRES_* database is configured.
local_postgres = _native_cluster


@pytest.fixture
async def dsn(request: pytest.FixtureRequest) -> str | None:
    """None selects the INTEGRATION_POSTGRES_* database; else a local cluster.

    tests/conftest.py gives the INTEGRATION_POSTGRES_* database the roles and
    the shared schema a real lane provisions before the node migrations run;
    a fresh cluster has neither, so they are provisioned here the same way.
    """
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


@pytest.mark.integration
class TestTheRowCarriesBackendIdAndHost:
    async def test_a_terminal_stores_backend_id_and_host(self, dsn: str | None) -> None:
        terminal = _quota_terminal()
        payload = _wire(terminal)
        payload["route"] = "local-qwen"
        payload["backend_id"] = "local-b"
        payload["host"] = "h202"
        cid = str(terminal.correlation_id)

        async with _runner(dsn) as (runner, admin_conn, _schema):
            assert await runner.project_event(
                runner._topic_delegation_failed,
                payload,
                MessageMeta(partition=0, offset=0, fallback_id=cid),
            )
            row = await admin_conn.fetchrow(_SELECT, cid)
            assert row is not None
            assert row["backend_id"] == "local-b"
            assert row["host"] == "h202"
            assert row["answering_backend"] == "local-qwen"

    async def test_a_terminal_without_them_and_a_blank_one_store_nulls(
        self, dsn: str | None
    ) -> None:
        bare = _quota_terminal()
        blank = _quota_terminal()
        blank_payload = _wire(blank)
        blank_payload["backend_id"] = "  "
        blank_payload["host"] = ""

        async with _runner(dsn) as (runner, admin_conn, _schema):
            for offset, payload, terminal in (
                (1, _wire(bare), bare),
                (2, blank_payload, blank),
            ):
                cid = str(terminal.correlation_id)
                assert await runner.project_event(
                    runner._topic_delegation_failed,
                    payload,
                    MessageMeta(partition=0, offset=offset, fallback_id=cid),
                )
                row = await admin_conn.fetchrow(_SELECT, cid)
                assert row is not None
                assert row["backend_id"] is None
                assert row["host"] is None

    async def test_null_backend_for_a_row_written_before_the_columns(
        self, dsn: str | None
    ) -> None:
        """A row that exists while the table has no such columns reads NULL in
        both once migration 0054 is applied: no default, no backfill, never ''.
        """
        terminal = _quota_terminal()
        payload = _wire(terminal)
        payload["backend_id"] = "local-b"
        payload["host"] = "h202"
        cid = str(terminal.correlation_id)

        async with _runner(dsn) as (runner, admin_conn, _schema):
            assert await runner.project_event(
                runner._topic_delegation_failed,
                payload,
                MessageMeta(partition=0, offset=3, fallback_id=cid),
            )
            # The table as it stood before 0054: the row is there, the columns are not.
            await admin_conn.execute(
                "ALTER TABLE delegation_events DROP COLUMN backend_id, DROP COLUMN host"
            )
            await admin_conn.execute(_MIGRATION_0054.read_text(encoding="utf-8"))
            row = await admin_conn.fetchrow(_SELECT, cid)
            assert row is not None
            assert row["backend_id"] is None
            assert row["host"] is None
