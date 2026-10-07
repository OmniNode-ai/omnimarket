# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20613 (T0.6): the ceiling budget-state write is atomic and replay-safe.

The writer used to read the period row, add the event's drawdown in Python and
write the sum back. Four properties follow from replacing that with one
transaction (insert the event's identity into ``delegation_budget_applied_events``,
then, only if inserted, one ``INSERT ... ON CONFLICT DO UPDATE`` that increments
the totals in the database):

* concurrent writers do not lose each other's drawdown;
* a replay of an event applied earlier (A, B, A) is not counted again;
* a failed transaction leaves neither the identity nor the totals behind;
* an event that happened earlier but arrives later does not move
  ``last_event_at`` backwards.

Real Postgres (a disposable native cluster), never a mock. SKIPS without
``initdb``/``pg_ctl`` on PATH.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator, Coroutine, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote_plus
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from tests.test_omn15909_real_postgres_projection_write_path_gate import (
    _APP_DASHBOARD_ROLE_SQL,
    _MIGRATIONS_DIR,
    _budgeted_tier,
)

_HANDLER = "omnimarket.nodes.node_projection_delegation.handlers.handler_delegation"
_TIER = "ceiling_budgeted"
_TENANT = "omninode"


# The budget surface only: the table (0019), its RLS (0041) and the applied-event
# identity table (0056) and its RLS (0057). The full node set also needs the pgcrypto extension,
# which a stock native cluster may not ship and this surface does not use.
_BUDGET_MIGRATIONS = (
    "0019_delegation_budget_state.sql",
    "0041_delegation_budget_state_rls_tenant_isolation.sql",
    "0056_delegation_budget_applied_events.sql",
    "0057_delegation_budget_applied_events_rls.sql",
)


@asynccontextmanager
async def _provisioned_runner(
    dsn: str,
) -> AsyncIterator[tuple[DelegationProjectionRunner, asyncpg.Connection, str]]:
    schema = f"omn20613_{uuid4().hex[:16]}"
    admin = await asyncpg.connect(dsn)
    pool: asyncpg.Pool | None = None
    try:
        await admin.execute(f"CREATE SCHEMA {schema}")
        await admin.execute(f"SET search_path TO {schema}, public")
        await admin.execute(_APP_DASHBOARD_ROLE_SQL)
        await admin.execute(
            "DO $$ BEGIN CREATE ROLE tenant_projection_writer NOLOGIN; "
            "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )
        for name in _BUDGET_MIGRATIONS:
            await admin.execute((_MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
        pool = await asyncpg.create_pool(
            dsn,
            min_size=1,
            max_size=8,
            server_settings={"search_path": f"{schema},public"},
        )
        adapter = AsyncpgAdapter(dsn=dsn)
        adapter._pool = pool
        runner = DelegationProjectionRunner()
        runner._db = adapter
        yield runner, admin, schema
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute("SET search_path TO public")
        await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin.close()


@pytest.fixture(scope="module")
def local_dsn() -> Iterator[str]:
    """The ``INTEGRATION_POSTGRES_*`` server when configured, else a disposable
    native PostgreSQL; a missing server is an explicit skip."""
    password = os.environ.get("INTEGRATION_POSTGRES_PASSWORD", "")
    if password:
        host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
        port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
        user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
        db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
        yield f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"
        return
    initdb, pg_ctl = shutil.which("initdb"), shutil.which("pg_ctl")
    if not initdb or not pg_ctl:
        pytest.skip("native initdb/pg_ctl unavailable")
    root = Path(tempfile.mkdtemp(prefix="omn20613-pg-"))
    data, sock = root / "data", root / "sock"
    sock.mkdir()
    env = {"PATH": os.environ.get("PATH", ""), "LANG": "C", "LC_ALL": "C"}
    try:
        init = subprocess.run(
            [initdb, "-D", str(data), "-U", "postgres", "--auth-local=trust"],
            capture_output=True,
            env=env,
        )
        if init.returncode:
            raise RuntimeError(f"initdb failed: {init.stderr.decode(errors='replace')}")
        start = subprocess.run(
            [
                pg_ctl,
                "-D",
                str(data),
                "-l",
                str(root / "postgres.log"),
                "-o",
                f"-k {sock} -h ''",
                "-w",
                "start",
            ],
            capture_output=True,
            env=env,
        )
        if start.returncode:
            raise RuntimeError(
                f"pg_ctl failed: {start.stderr.decode(errors='replace')}"
            )
        yield f"postgresql://postgres@/postgres?host={sock}"
    finally:
        if data.exists():
            subprocess.run(
                [pg_ctl, "-D", str(data), "-m", "immediate", "-w", "stop"],
                check=False,
                capture_output=True,
                env=env,
            )
        shutil.rmtree(root, ignore_errors=True)


def _apply(
    runner: DelegationProjectionRunner,
    correlation_id: str,
    *,
    drawdown: float = 0.01,
    overage: float = 0.02,
    timestamp: str = "2026-10-05T12:00:00+00:00",
) -> Coroutine[object, object, None]:
    return runner._materialize_budget_state_async(
        correlation_id=correlation_id,
        cost_tier_name=_TIER,
        cost_measurement_source="measured",
        budget_headroom_consumed_usd=drawdown,
        cost_usd=overage,
        tenant_id=_TENANT,
        timestamp=timestamp,
    )


async def _state(conn: asyncpg.Connection) -> asyncpg.Record | None:
    return await conn.fetchrow(
        "SELECT * FROM delegation_budget_state WHERE tenant_id = $1", _TENANT
    )


async def _applied(conn: asyncpg.Connection) -> int:
    return int(
        await conn.fetchval("SELECT count(*) FROM delegation_budget_applied_events")
    )


@pytest.mark.integration
class TestBudgetStateAtomicWrite:
    async def test_concurrent_writers_lose_no_drawdown(self, local_dsn: str) -> None:
        async with _provisioned_runner(local_dsn) as (runner, conn, _schema):
            with patch(f"{_HANDLER}.resolve_tier_cost", return_value=_budgeted_tier()):
                await asyncio.gather(
                    *[_apply(runner, f"c-{i}-{uuid4().hex}") for i in range(20)]
                )
            row = await _state(conn)
            assert row is not None
            assert row["delegation_count"] == 20
            assert row["consumed_usd"] == Decimal("0.200000")
            assert row["overage_usd"] == Decimal("0.400000")
            assert row["headroom_remaining_usd"] == Decimal("0.800000")
            assert await _applied(conn) == 20

    async def test_a_b_a_replay_counts_each_event_once(self, local_dsn: str) -> None:
        async with _provisioned_runner(local_dsn) as (runner, conn, _schema):
            with patch(f"{_HANDLER}.resolve_tier_cost", return_value=_budgeted_tier()):
                await _apply(runner, "event-a", drawdown=0.10, overage=0.0)
                await _apply(runner, "event-b", drawdown=0.20, overage=0.0)
                await _apply(runner, "event-a", drawdown=0.10, overage=0.0)
            row = await _state(conn)
            assert row is not None
            assert row["delegation_count"] == 2
            assert row["consumed_usd"] == Decimal("0.300000")
            assert row["headroom_remaining_usd"] == Decimal("0.700000")
            assert await _applied(conn) == 2

    async def test_failed_transaction_leaves_nothing_behind(
        self, local_dsn: str
    ) -> None:
        async with _provisioned_runner(local_dsn) as (runner, conn, _schema):
            with patch(f"{_HANDLER}.resolve_tier_cost", return_value=_budgeted_tier()):
                # NUMERIC(18, 6) cannot hold 1e13: the totals statement fails
                # AFTER the identity insert, so the identity must roll back too.
                with pytest.raises(asyncpg.PostgresError):
                    await _apply(runner, "poison", drawdown=1e13)
                assert await _state(conn) is None
                assert await _applied(conn) == 0
                # The same event, retried with a valid amount, still applies.
                await _apply(runner, "poison", drawdown=0.05)
            row = await _state(conn)
            assert row is not None
            assert row["delegation_count"] == 1
            assert row["consumed_usd"] == Decimal("0.050000")
            assert await _applied(conn) == 1

    async def test_out_of_order_timestamps_keep_the_greater_last_event_at(
        self, local_dsn: str
    ) -> None:
        late, early = "2026-10-05T18:00:00+00:00", "2026-10-05T09:00:00+00:00"
        async with _provisioned_runner(local_dsn) as (runner, conn, _schema):
            with patch(f"{_HANDLER}.resolve_tier_cost", return_value=_budgeted_tier()):
                await _apply(runner, "happened-late", timestamp=late)
                await _apply(runner, "happened-early", timestamp=early)
            row = await _state(conn)
            assert row is not None
            assert row["delegation_count"] == 2
            assert row["last_event_at"] == datetime.fromisoformat(late).astimezone(UTC)
            assert row["first_event_at"] == datetime.fromisoformat(early).astimezone(
                UTC
            )
            assert row["last_correlation_id"] == "happened-late"

    async def test_the_apply_runs_on_the_applied_events_write_binding(
        self, local_dsn: str
    ) -> None:
        class _Bindings:
            def write_binding_for(self, table: str) -> str:
                return f"write:{table}"

            def read_binding_for(self, table: str) -> str:
                raise AssertionError("the budget apply is a write")

        async with _provisioned_runner(local_dsn) as (runner, conn, _schema):
            routed = {f"write:{runner._table_budget_applied_events}": runner._db}
            runner._standalone_bindings = _Bindings()
            runner._db_by_binding = routed
            with patch(f"{_HANDLER}.resolve_tier_cost", return_value=_budgeted_tier()):
                await _apply(runner, "routed-by-binding")
            row = await _state(conn)
            assert row is not None
            assert row["delegation_count"] == 1
            assert await _applied(conn) == 1
