# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19961: real-Postgres proof of the lab container memory upsert.

The unit tests drive the writer against a keyed double, which accepts a bound
parameter of any Python type and applies ON CONFLICT by assumption. Only a
real Postgres connection enforces the column types through asyncpg's extended
query protocol (TIMESTAMPTZ from an aware datetime, JSONB from a JSON string,
BIGINT for byte counts past 2**31) and proves the conflict arm itself: that a
replay of one event leaves count(*) equal to count(DISTINCT record_key), which
is the ticket's AC2 falsifier.

The harness mirrors ``tests/test_omn18768_runner_fleet_real_postgres_write_path.py``:
it SKIPS (never ERRORs) without a reachable database and provisions its own
throwaway schema, applying the node's real 0000 migration into it.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote_plus

import asyncpg
import pytest

from omnimarket.nodes.node_projection_lab_container_memory.handlers.handler_container_memory_fold import (
    HandlerContainerMemoryFold,
)
from omnimarket.nodes.node_projection_lab_container_memory.handlers.handler_container_memory_writer import (
    _UPSERT,
    upsert_params,
)
from omnimarket.nodes.node_projection_lab_container_memory.models import (
    ModelLaneContainerMemoryEvent,
)

_NODE_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_lab_container_memory"
)
_MIGRATION = _NODE_DIR / "migrations" / "0000_create_lab_container_memory_window.sql"
_FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "lab_container_memory"
    / "event.v1.json"
)
_SCHEMA = "omn19961_lab_container_memory_test"
_T0 = datetime(2026, 9, 28, 20, 0, 0, tzinfo=UTC)


async def _connect_or_skip() -> asyncpg.Connection:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD not set -- skipping lab container memory DB proof"
        )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    dsn = f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"
    try:
        return await asyncpg.connect(dsn)
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover
        pytest.skip(f"no reachable Postgres for lab container memory proof: {exc}")


def _scoped(statement: str) -> str:
    return statement.replace("omninode_internal.", f"{_SCHEMA}.")


async def _setup(conn: asyncpg.Connection) -> None:
    await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await conn.execute(f"CREATE SCHEMA {_SCHEMA}")
    await conn.execute(_scoped(_MIGRATION.read_text(encoding="utf-8")))


async def _apply(conn: asyncpg.Connection) -> int:
    event = ModelLaneContainerMemoryEvent.model_validate(
        json.loads(_FIXTURE.read_text(encoding="utf-8"))
    )
    written = 0
    for row in HandlerContainerMemoryFold().handle(event).rows:
        returned = await conn.fetch(_scoped(_UPSERT), *upsert_params(row, _T0))
        written += len(returned)
    return written


@pytest.mark.integration
async def test_replaying_one_event_leaves_one_row_per_record_key() -> None:
    conn = await _connect_or_skip()
    try:
        await _setup(conn)
        assert await _apply(conn) == 2
        assert await _apply(conn) == 2
        total, distinct = await conn.fetchrow(
            f"SELECT count(*), count(DISTINCT record_key) "
            f"FROM {_SCHEMA}.lab_container_memory_window"
        )
        assert (total, distinct) == (2, 2)
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()


@pytest.mark.integration
async def test_real_postgres_stores_the_writers_bound_types() -> None:
    conn = await _connect_or_skip()
    try:
        await _setup(conn)
        await _apply(conn)
        redpanda = await conn.fetchrow(
            f"SELECT * FROM {_SCHEMA}.lab_container_memory_window "
            "WHERE container_name = $1",
            "omnibase-infra-sim-202-redpanda",
        )
        assert redpanda is not None
        # No memory limit is a NULL, not a zero.
        assert redpanda["limit_bytes"] is None
        # Past 2**31: BIGINT, not INTEGER.
        assert redpanda["peak_bytes"] == 2114498560
        assert isinstance(redpanda["window_end"], datetime)
        assert redpanda["peak_window_end"] == redpanda["window_end"]
        runs = json.loads(redpanda["ci_runs"])
        assert {run["run_id"] for run in runs} == {"36468089372", "36454760449"}
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()


@pytest.mark.integration
async def test_the_writer_entry_writes_real_rows_from_inside_a_running_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runtime's entry, end to end against real Postgres.

    ``handle()`` is synchronous and this test calls it from inside a running
    event loop, which is the case the entry's loop guard exists for: without
    it ``asyncio.run`` raises before a row is written. It is called twice with
    the same event, so the replay rule is proven through the real adapter too.
    """
    from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
    from omnimarket.nodes.node_projection_lab_container_memory.handlers import (
        handler_container_memory_writer as writer_module,
    )

    conn = await _connect_or_skip()
    try:
        await _setup(conn)
        password = os.environ.get(
            "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
        )
        dsn = "postgresql://{}:{}@{}:{}/{}".format(
            quote_plus(os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")),
            quote_plus(password),
            os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost"),
            os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"),
            os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra"),
        )
        monkeypatch.setattr(writer_module, "_UPSERT", _scoped(_UPSERT))
        monkeypatch.setenv("OMNIDASH_ANALYTICS_DB_URL", dsn)
        writer = writer_module.LabContainerMemoryProjectionWriter()
        writer._db = AsyncpgAdapter(dsn=dsn, min_size=1, max_size=1)
        event = json.loads(_FIXTURE.read_text(encoding="utf-8"))

        first = writer.handle(dict(event))
        second = writer.handle(dict(event))

        assert first["rows_upserted"] == 2
        assert second["rows_upserted"] == 2
        total, distinct = await conn.fetchrow(
            f"SELECT count(*), count(DISTINCT record_key) "
            f"FROM {_SCHEMA}.lab_container_memory_window"
        )
        assert (total, distinct) == (2, 2)
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()
