# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19986 real writer proof: asyncpg types, the earliest mint wins, replays and unknown tenants.

Runs the real writer as the integration DSN's user in a disposable schema, with
the node's own migrations and no test-local grants. Skipped without an
integration DSN; hosted CI runs it.

Failure modes each test is written against:

* the writer stores a second row for a replayed mint, or moves the mint time to
  a later event's;
* an exact replay publishes nothing, so a publish lost after commit is never
  recovered; a later mint republishes a row that did not change;
* a tenant the registry mirror does not record is stored;
* the columns are not the declared Postgres types.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote_plus
from uuid import UUID, uuid4

import asyncpg
import pytest

from omnimarket.nodes.node_projection_local_identity.handlers import (
    handler_local_identity_writer as writer_module,
)
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.tenant_registry_resolution import (
    TenantRegistryResolutionError,
)

_MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_local_identity/migrations"
)
_TENANT = "6c24401e-0000-4000-8000-00000000000a"
_UNKNOWN = "11111111-2222-4333-8444-555555555555"
_AT = datetime(2026, 10, 30, 5, tzinfo=UTC)


def _dsn_or_skip() -> str:
    dsn = os.environ.get("INTEGRATION_POSTGRES_DSN")
    if dsn:
        return dsn
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip("INTEGRATION_POSTGRES DSN/password not set")
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = quote_plus(os.environ.get("INTEGRATION_POSTGRES_USER", "postgres"))
    database = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{user}:{quote_plus(password)}@{host}:{port}/{database}"


@pytest.mark.integration
async def test_real_postgres_writer_keeps_one_row_with_the_earliest_mint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = _dsn_or_skip()
    conn = await asyncpg.connect(dsn)
    schema = f"omn19986_writer_{uuid4().hex}"
    pool: asyncpg.Pool | None = None
    published: list[dict[str, Any]] = []

    async def capture(exposure: Any, **kwargs: Any) -> bool:
        published.append({"topic": exposure.topic, **kwargs})
        return True

    try:
        await conn.execute(f"CREATE SCHEMA {schema}")
        for migration in sorted(_MIGRATIONS.glob("*.sql")):
            await conn.execute(
                migration.read_text()
                .replace("public.", f"{schema}.")
                .replace("ON SCHEMA public ", f"ON SCHEMA {schema} ")
            )
        await conn.execute(
            f"CREATE TABLE {schema}.tenant_registry_mirror ("
            "tenant_slug TEXT PRIMARY KEY, tenant_uuid UUID NOT NULL, "
            "status TEXT NOT NULL, observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
        )
        await conn.execute(
            f"INSERT INTO {schema}.tenant_registry_mirror "
            "(tenant_slug, tenant_uuid, status) VALUES ($1, $2, 'active')",
            "omn19986-writer",
            UUID(_TENANT),
        )
        pool = await asyncpg.create_pool(
            dsn,
            min_size=1,
            max_size=1,
            server_settings={"search_path": f"{schema}, public"},
        )
        assert pool is not None
        for name in ("_SELECT_ROW", "_UPSERT_ROW"):
            monkeypatch.setattr(
                writer_module,
                name,
                getattr(writer_module, name).replace("public.", f"{schema}."),
            )
        writer = writer_module.LocalIdentityProjectionWriter()
        monkeypatch.setattr(writer, "_db", SimpleNamespace(pool=pool))
        monkeypatch.setattr(writer, "publish_snapshot_delta", capture)
        topic = writer.subscribe_topics[0]
        meta = MessageMeta(partition=0, offset=3, fallback_id="proof", topic=topic)

        def mint(tenant: str, minutes: int) -> dict[str, Any]:
            return {
                "tenant_id": tenant,
                "minted_at": (_AT + timedelta(minutes=minutes)).isoformat(),
            }

        async def rows() -> list[dict[str, Any]]:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)", _TENANT
                )
                return [
                    dict(r)
                    for r in await conn.fetch(f"SELECT * FROM {schema}.local_identity")
                ]

        assert await writer.project_event(topic, mint(_TENANT, 0), meta)
        first = await rows()
        assert len(first) == 1
        assert first[0]["minted_at"] == _AT
        assert first[0]["minted_at"].tzinfo is not None
        assert isinstance(first[0]["projection_cursor"], int)
        types = {
            r["column_name"]: r["data_type"]
            for r in await conn.fetch(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = $1 AND table_name = 'local_identity'",
                schema,
            )
        }
        assert types["minted_at"] == "timestamp with time zone"
        assert types["projection_cursor"] == "bigint"
        assert len(published) == 1
        original = published[0]

        published.clear()
        assert await writer.project_event(topic, mint(_TENANT, 0), meta)
        assert await rows() == first
        assert published == [original]

        published.clear()
        assert await writer.project_event(topic, mint(_TENANT, 7), meta)
        assert await rows() == first
        assert published == []

        assert await writer.project_event(topic, mint(_TENANT, -3), meta)
        earlier = await rows()
        assert len(earlier) == 1
        assert earlier[0]["minted_at"] == _AT - timedelta(minutes=3)
        assert earlier[0]["projection_cursor"] > first[0]["projection_cursor"]

        with pytest.raises(TenantRegistryResolutionError):
            await writer.project_event(topic, mint(_UNKNOWN, 0), meta)
        assert len(await rows()) == 1
    finally:
        if pool is not None:
            await pool.close()
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()
