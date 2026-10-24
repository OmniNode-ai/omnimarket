"""OMN-19448: both writers store experiments under the migrated closed CHECK.

Uses the full migration chain and the existing PostgreSQL availability policy:
the connection comes from the INTEGRATION_POSTGRES_* variables through the
OMN-15909 gate's ``_base_dsn``, and the test skips when none is reachable.
"""

from __future__ import annotations

import asyncio

import asyncpg
import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.projection.postgres_sync_database import PostgresSyncProjectionAdapter
from omnimarket.projection.runner import MessageMeta
from tests.helpers.tenant_registry import PROJECTION_TENANT_SLUG, PROJECTION_TENANT_UUID
from tests.test_omn15909_real_postgres_projection_write_path_gate import (
    _MIGRATIONS_DIR,
    _base_dsn,
    _provisioned_runner,
)
from tests.unit.projection.test_delegation_data_source_experiment_omn19448 import (
    recorded_terminal,
)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["sync", "async"])
async def test_synthetic_terminal_and_closed_check(writer: str) -> None:
    async with _provisioned_runner() as (runner, conn, schema):
        # This mirror belongs to its own node; the delegation chain reads it.
        registry_migration = (
            _MIGRATIONS_DIR.parent.parent
            / "node_projection_tenant_registry/migrations/0000_create_tenant_registry_mirror.sql"
        )
        await conn.execute(registry_migration.read_text())
        await conn.execute(
            "INSERT INTO tenant_registry_mirror (tenant_slug, tenant_uuid, status) "
            "VALUES ($1, $2, 'active')",
            PROJECTION_TENANT_SLUG,
            PROJECTION_TENANT_UUID,
        )
        payload = recorded_terminal("synthetic")
        payload["tenant_id"] = str(PROJECTION_TENANT_UUID)
        cid = payload["correlation_id"]
        if writer == "async":
            assert await runner.project_event(
                runner._topic_delegate_skill_completed,
                payload,
                MessageMeta(partition=0, offset=0, fallback_id=cid),
            )
        else:
            db = PostgresSyncProjectionAdapter(_base_dsn(), schema=schema)
            await asyncio.to_thread(
                HandlerProjectionDelegation().handle, {**payload, "_db": db}
            )
        assert (
            await conn.fetchval(
                "SELECT data_source FROM delegation_events WHERE correlation_id = $1",
                cid,
            )
            == "experiment"
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE delegation_events SET data_source = 'bogus' WHERE correlation_id = $1",
                cid,
            )
        # The new migration is repeatable without validating/scanning old rows.
        sql = (
            _MIGRATIONS_DIR / "0064_delegation_events_data_source_experiment.sql"
        ).read_text()
        await conn.execute(sql)
        assert (
            await conn.fetchval(
                "SELECT convalidated FROM pg_constraint WHERE conrelid = 'delegation_events'::regclass "
                "AND conname = 'delegation_events_data_source_check'",
            )
            is False
        )
