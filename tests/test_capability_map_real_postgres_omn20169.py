# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Capability cells through the runtime writer and real PostgreSQL migrations."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from uuid import UUID, uuid4

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_delegation_capability_map.handlers import (
    HandlerDelegationCapabilityMapWriter,
)
from omnimarket.nodes.node_projection_delegation_capability_map.lab_run import (
    build_map_run,
)

_MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_delegation_capability_map/migrations"
)
_OBSERVED = datetime(2026, 10, 1, tzinfo=UTC)


def _dsn(database: str, user: str, password: str) -> str:
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    return (
        f"postgresql://{quote_plus(user)}:{quote_plus(password)}"
        f"@{host}:{port}/{quote_plus(database)}"
    )


@asynccontextmanager
async def _migrated_writer() -> AsyncIterator[
    tuple[HandlerDelegationCapabilityMapWriter, AsyncpgAdapter]
]:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip("INTEGRATION_POSTGRES_PASSWORD not set for real PostgreSQL proof")
    admin_user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    base_database = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    suffix = uuid4().hex
    database = f"capability_map_{suffix}"
    role = f"capability_writer_{suffix}"
    role_password = uuid4().hex
    admin = await asyncpg.connect(_dsn(base_database, admin_user, password))
    migration_connection: asyncpg.Connection | None = None
    reader = AsyncpgAdapter(dsn=_dsn(database, role, role_password))
    try:
        # Each test owns a database because the shipped SQL explicitly names
        # public. Never rewrite that SQL or touch a shared public table.
        await admin.execute(f"CREATE DATABASE {database}")
        await admin.execute(
            f"CREATE ROLE {role} LOGIN NOSUPERUSER NOBYPASSRLS "
            f"NOCREATEDB NOCREATEROLE PASSWORD '{role_password}'"
        )
        migration_connection = await asyncpg.connect(
            _dsn(database, admin_user, password)
        )
        # The node migrations depend on these infra-owned roles. Leave any
        # pre-existing role unchanged, including its login and membership.
        for dependency in ("app_dashboard", "tenant_projection_writer"):
            await migration_connection.execute(
                f"""DO $$ BEGIN
                    CREATE ROLE {dependency} NOLOGIN NOSUPERUSER NOBYPASSRLS;
                EXCEPTION WHEN duplicate_object OR unique_violation THEN NULL;
                END $$"""
            )
        for migration in sorted(_MIGRATIONS.glob("*.sql")):
            await migration_connection.execute(migration.read_text())
        await admin.execute(f"GRANT tenant_projection_writer TO {role}")
        writer = HandlerDelegationCapabilityMapWriter()
        writer.bind_projection_database_url(_dsn(database, role, role_password))
        await reader.connect()
        identity = await reader.execute(
            "SELECT current_user AS name, rolsuper, rolbypassrls "
            "FROM pg_roles WHERE rolname = current_user"
        )
        assert identity == [{"name": role, "rolsuper": False, "rolbypassrls": False}]
        yield writer, reader
    finally:
        await reader.close()
        if migration_connection is not None:
            await migration_connection.close()
        try:
            await admin.execute(f"DROP DATABASE IF EXISTS {database}")
            await admin.execute(f"DROP ROLE IF EXISTS {role}")
        finally:
            await admin.close()


def _payload(tenant: UUID, *, adequate: int = 6) -> dict[str, Any]:
    items = [
        {
            "item_key": f"review-{index}",
            "task_class": "code_review",
            "label": "adequate" if index < adequate else "inadequate",
            "prompt_snapshot": "Review: def increment(x): return x + 1",
            "observed_at": _OBSERVED,
            "rubric_version": "test-rubric-v1",
            "rater_role": "test-grader",
            "model_key": "test-model",
            "backend_id": "test-backend",
        }
        for index in range(8)
    ]
    run, skipped = build_map_run(tenant, items)
    assert skipped == {}
    return run.model_dump(mode="json")


@pytest.mark.integration
async def test_capability_cells_round_trip_and_ordering_on_real_postgres() -> None:
    tenant = uuid4()
    payload = _payload(tenant)
    async with _migrated_writer() as (writer, reader):
        first = await asyncio.to_thread(writer.handle, payload)
        assert first["rows_upserted"] == first["rows_handled"] == 1
        assert not writer.db.is_connected
        rows = await reader.execute(
            "SELECT * FROM public.delegation_capability_cells", tenant=str(tenant)
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["tenant_id"] == tenant
        assert row["map_run_id"] == payload["map_run_id"]
        assert row["model_key"] == "test-model"
        assert row["backend_id"] == "test-backend"
        assert row["task_class"] == "code_review"
        assert row["size_band"] == "S"
        assert (row["n"], row["passes"], row["pass_rate"]) == (8, 6, 0.75)
        assert row["observed_at"] == _OBSERVED
        assert row["verdict"] == "REFUSED"
        assert row["required_n"] > row["n"]
        assert 0 <= row["wilson_low"] < row["pass_rate"] < row["wilson_high"] <= 1
        assert 0 <= row["decision_lower_bound"] <= row["pass_rate"]
        configuration = payload["rows"][0]["configuration"]
        assert (
            json.loads(row["configuration_parameters"]) == configuration["parameters"]
        )

        duplicate = await asyncio.to_thread(writer.handle, payload)
        assert duplicate["rows_upserted"] == 0
        assert duplicate["rows_refused_by_ordering_guard"] == 1
        assert duplicate["rows_handled"] == 1

        updated = _payload(tenant, adequate=8)
        updated["map_run_id"] = payload["map_run_id"]
        updated["observed_at"] = (_OBSERVED + timedelta(seconds=1)).isoformat()
        result = await asyncio.to_thread(writer.handle, updated)
        assert result["rows_upserted"] == 1
        newer = await reader.execute(
            "SELECT * FROM public.delegation_capability_cells", tenant=str(tenant)
        )
        assert len(newer) == 1
        assert newer[0]["configuration_id"] == row["configuration_id"]
        assert (newer[0]["passes"], newer[0]["pass_rate"]) == (8, 1.0)
        assert newer[0]["observed_at"] == _OBSERVED + timedelta(seconds=1)
        stale = await asyncio.to_thread(writer.handle, payload)
        assert stale["rows_upserted"] == 0
        assert stale["rows_refused_by_ordering_guard"] == 1
        assert (
            await reader.execute(
                "SELECT * FROM public.delegation_capability_cells", tenant=str(tenant)
            )
            == newer
        )


@pytest.mark.integration
async def test_capability_cells_are_tenant_scoped_on_real_postgres() -> None:
    tenant, other = uuid4(), uuid4()
    payload = _payload(tenant)
    async with _migrated_writer() as (writer, reader):
        await asyncio.to_thread(writer.handle, payload)
        assert (
            await reader.execute(
                "SELECT * FROM public.delegation_capability_cells", tenant=str(other)
            )
            == []
        )
        other_payload = _payload(other, adequate=8)
        other_payload["map_run_id"] = payload["map_run_id"]
        result = await asyncio.to_thread(writer.handle, other_payload)
        assert result["rows_upserted"] == 1
        for owner, passes in ((tenant, 6), (other, 8)):
            rows = await reader.execute(
                "SELECT tenant_id, map_run_id, passes "
                "FROM public.delegation_capability_cells",
                tenant=str(owner),
            )
            assert rows == [
                {
                    "tenant_id": owner,
                    "map_run_id": payload["map_run_id"],
                    "passes": passes,
                }
            ]
        # Positive reads above distinguish isolation from an empty/broken table.
        # The same restricted role must also reject a mismatched write context.
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await reader.execute(
                "UPDATE public.delegation_capability_cells SET tenant_id = $1 "
                "WHERE tenant_id = $2",
                other,
                tenant,
                tenant=str(tenant),
            )
