# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Graded-run cells through the runtime writer and real PostgreSQL migrations.

A graded run whose every row names the backend asked, its host and the backend
that answered writes a MET cell; one whose rows name no answering backend
writes none and is counted as identity_field_missing.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from uuid import UUID, uuid4

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.models.delegation_capability_map.model_capability_configuration import (
    ModelCapabilityConfiguration,
)
from omnimarket.models.delegation_capability_map.model_capability_grid import (
    ModelCapabilityGridGrade,
    ModelCapabilityGridItem,
    ModelCapabilityGridModel,
)
from omnimarket.nodes.node_projection_delegation_capability_map.handlers import (
    HandlerDelegationCapabilityMapWriter,
)
from omnimarket.nodes.node_projection_delegation_capability_map.lab_grid_run import (
    build_grid_map_run,
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
    database = f"capability_grid_{suffix}"
    role = f"capability_grid_writer_{suffix}"
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


_MODEL = ModelCapabilityGridModel(
    model_key="test-model", backend_id="test-backend", model_revision="test-model"
)


def _payload(tenant: UUID, answering_backend: str | None) -> dict[str, Any]:
    items = tuple(
        ModelCapabilityGridItem(
            item_key=f"review-{index}",
            task_class="review",
            size_band=EnumSizeBand.S,
            prompt=f"Review change {index}",
        )
        for index in range(100)
    )
    grades = {
        (item.item_key, _MODEL.backend_id): ModelCapabilityGridGrade(
            passed=True,
            host="model-host.example",
            answering_backend=answering_backend,
            served_model="test-model",
        )
        for item in items
    }
    run, counts = build_grid_map_run(
        tenant_id=tenant,
        items=items,
        models=(_MODEL,),
        configuration=ModelCapabilityConfiguration(),
        grades=grades,
        suite_revision="test-rubric-v1",
        grader_revision="test-grader",
        observed_at=_OBSERVED,
    )
    assert counts["verdict_rows"] == 100
    return run.model_dump(mode="json")


@pytest.mark.integration
async def test_graded_run_writes_a_met_cell_on_real_postgres() -> None:
    tenant = uuid4()
    payload = _payload(tenant, "test-backend")
    assert all(row["host"] and row["answering_backend"] for row in payload["rows"])
    async with _migrated_writer() as (writer, reader):
        result = await asyncio.to_thread(writer.handle, payload)
        assert result["rows_upserted"] == result["cells"] == 1
        assert result["inadmissible"] == {}
        rows = await reader.execute(
            "SELECT backend_id, task_class, size_band, n, passes, verdict "
            "FROM public.delegation_capability_cells",
            tenant=str(tenant),
        )
        assert rows == [
            {
                "backend_id": "test-backend",
                "task_class": "review",
                "size_band": "S",
                "n": 100,
                "passes": 100,
                "verdict": "MET",
            }
        ]


@pytest.mark.integration
async def test_graded_run_with_no_answering_backend_writes_no_cell_on_real_postgres() -> (
    None
):
    tenant = uuid4()
    payload = _payload(tenant, None)
    async with _migrated_writer() as (writer, reader):
        result = await asyncio.to_thread(writer.handle, payload)
        assert result["cells"] == 0
        assert result["inadmissible"] == {"identity_field_missing": 100}
        assert (
            await reader.execute(
                "SELECT * FROM public.delegation_capability_cells", tenant=str(tenant)
            )
            == []
        )
