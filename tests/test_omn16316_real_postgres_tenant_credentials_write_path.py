# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16316: real-Postgres write-path gate for the tenant-credentials projection.

Required companion under this repo's projection-write-path real-DB gate
(``scripts/ci/check_projection_write_path_db_gate.py``, OMN-15909) because
this PR touches ``handler_tenant_credentials_projection.py``. The mock-DB
unit tests in ``tests/test_omn16316_tenant_credentials_projection.py`` prove
the SQL text and bound-argument shape; only a real Postgres connection
enforces actual column types via asyncpg's extended query protocol (the
OMN-15905 failure class this gate exists to catch).

Harness mirrors ``tests/test_omn16150_publish_snapshot_delta_delete_real_postgres.py``
byte-for-byte in spirit: disposable per-run schema, real ``AsyncpgAdapter``,
SKIPS (never ERRORs) without a reachable database.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote_plus
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_projection import (
    HandlerTenantCredentialsProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_tenant_credentials"
    / "migrations"
    / "0000_create_tenant_inference_credentials.sql"
)

TOPIC_REGISTERED = "onex.evt.omnimarket.credential-registered.v1"
TOPIC_REVOKED = "onex.evt.omnimarket.credential-revoked.v1"


def _base_dsn() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


async def _connect_or_skip() -> asyncpg.Connection:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "POSTGRES_PASSWORD not set -- skipping OMN-16316 real-Postgres "
            "tenant-credentials write-path proof"
        )
    try:
        return await asyncpg.connect(_base_dsn())
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for OMN-16316 write-path proof: {exc}")


@asynccontextmanager
async def _provisioned_runner() -> AsyncIterator[
    tuple[HandlerTenantCredentialsProjectionRunner, asyncpg.Connection, str]
]:
    admin_conn = await _connect_or_skip()
    schema = f"omn16316_{uuid4().hex[:16]}"
    pool: asyncpg.Pool | None = None
    try:
        await admin_conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin_conn.execute(f"CREATE SCHEMA {schema}")
        await admin_conn.execute(f"SET search_path TO {schema}, public")
        await admin_conn.execute(_MIGRATION.read_text(encoding="utf-8"))

        pool = await asyncpg.create_pool(
            _base_dsn(),
            min_size=1,
            max_size=3,
            server_settings={"search_path": f"{schema},public"},
        )
        adapter = AsyncpgAdapter(dsn=_base_dsn())
        adapter._pool = pool

        runner = HandlerTenantCredentialsProjectionRunner()
        runner._db = adapter

        yield runner, admin_conn, schema
    finally:
        if pool is not None:
            with contextlib.suppress(Exception):
                await pool.close()
        with contextlib.suppress(Exception):
            await admin_conn.execute("SET search_path TO public")
            await admin_conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin_conn.close()


@pytest.mark.integration
class TestRealPostgresWritePath:
    async def test_registered_inserts_a_correctly_typed_row(self) -> None:
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            ref = f"cred_omninode_openrouter_{uuid4().hex[:12]}"
            ok = await runner.project_event(
                TOPIC_REGISTERED,
                {
                    "tenant_id": "omninode",
                    "provider": "openrouter",
                    "name": "real-pg-proof",
                    "api_key_ref": ref,
                },
                MessageMeta(
                    partition=0, offset=0, fallback_id=ref, topic=TOPIC_REGISTERED
                ),
            )
            assert ok is True

            row = await admin_conn.fetchrow(
                "SELECT api_key_ref, tenant_id, name, provider, created_at, "
                "revoked_at FROM tenant_inference_credentials WHERE api_key_ref = $1",
                ref,
            )
            assert row is not None
            assert row["api_key_ref"] == ref
            assert row["tenant_id"] == "omninode"
            assert row["name"] == "real-pg-proof"
            assert row["provider"] == "openrouter"
            assert row["created_at"] is not None
            assert row["revoked_at"] is None

    async def test_revoked_sets_revoked_at_without_deleting_the_row(self) -> None:
        async with _provisioned_runner() as (runner, admin_conn, _schema):
            ref = f"cred_omninode_openai_{uuid4().hex[:12]}"
            await runner.project_event(
                TOPIC_REGISTERED,
                {
                    "tenant_id": "omninode",
                    "provider": "openai",
                    "name": "real-pg-revoke-proof",
                    "api_key_ref": ref,
                },
                MessageMeta(
                    partition=0, offset=0, fallback_id=ref, topic=TOPIC_REGISTERED
                ),
            )

            ok = await runner.project_event(
                TOPIC_REVOKED,
                {"tenant_id": "omninode", "api_key_ref": ref},
                MessageMeta(
                    partition=0, offset=1, fallback_id=ref, topic=TOPIC_REVOKED
                ),
            )
            assert ok is True

            row = await admin_conn.fetchrow(
                "SELECT api_key_ref, revoked_at FROM tenant_inference_credentials "
                "WHERE api_key_ref = $1",
                ref,
            )
            assert row is not None, "revoke must never delete the row"
            assert row["revoked_at"] is not None

    async def test_revoke_of_unknown_ref_matches_zero_rows_not_an_error(self) -> None:
        async with _provisioned_runner() as (runner, _admin_conn, _schema):
            ok = await runner.project_event(
                TOPIC_REVOKED,
                {"tenant_id": "omninode", "api_key_ref": "cred_never_registered"},
                MessageMeta(
                    partition=0,
                    offset=0,
                    fallback_id="never",
                    topic=TOPIC_REVOKED,
                ),
            )
            assert ok is True
