# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19972: on a real Postgres, the run-trace writer stores a slug step under the mirror's uuid.

The writer itself, not its statements, runs against a database that has the
run_trace migrations and the tenant mirror's own migration applied, as a role
that row-level security binds. A uuid-scoped read is then the reader's read.
SKIPS without a reachable database; it needs a database of its own, because the
writer names ``public``.

Failure modes each test is written against:

* (F1) the slug is stored, so a read scoped by the tenant's uuid serves nothing
  (omnibase_infra#5041's lab result);
* (F2) an unknown slug writes a row under some tenant instead of being refused;
* (F5) the session's tenant and the row's disagree, so the forced row-level
  security refuses the insert;
* (F6) another tenant's step with the same run id and sequence joins this
  tenant's run;
* (F7) the writer only works because its role was granted the mirror by hand, which no lane does (the deployed role holds nothing on it).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import asyncpg
import pytest

from omnimarket.nodes.node_projection_run_trace.handlers.handler_run_trace_writer import (
    RunTraceProjectionWriter,
)
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.tenant_registry_resolution import (
    TenantRegistryResolutionError,
)

_ROOT = Path(__file__).resolve().parents[1]
_NODES = _ROOT / "src/omnimarket/nodes"
_MIGRATIONS = (
    _NODES
    / "node_projection_tenant_registry/migrations/0000_create_tenant_registry_mirror.sql",
    _NODES / "node_projection_run_trace/migrations/0000_create_run_trace.sql",
    _NODES
    / "node_projection_run_trace/migrations/0001_grant_tenant_projection_writer_run_trace.sql",
    _NODES / "node_projection_run_trace/migrations/0002_force_rls_run_trace.sql",
)
_CHAIN = json.loads(
    (_ROOT / "tests/fixtures/omn19987/run_step_chain.json").read_text(encoding="utf-8")
)
_SLUG_A, _UUID_A = "local-ee67b16e7aab", "97b9aa50-f53f-adb8-5404-5c7318dd4cb1"
_SLUG_B, _UUID_B = "local-0b1c2d3e4f50", "11111111-2222-4333-8444-555555555555"
_ROLE = "tenant_projection_writer"
_MIRROR_ROLE = "app_dashboard"
# Every tenant this test can write under, by uuid or by slug.
_OWN_TENANTS = [_UUID_A, _UUID_B, _SLUG_A, _SLUG_B, "local-nobody-minted"]
_META = MessageMeta(
    partition=0,
    offset=3,
    fallback_id="run-step-0-3",
    topic="onex.evt.omnimarket.delegation-run-step.v1",
)

pytestmark = pytest.mark.integration


def _dsn_or_skip() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD not set -- skipping run-trace tenant proof"
        )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


async def _owner_or_skip() -> asyncpg.Connection:
    try:
        return await asyncpg.connect(_dsn_or_skip())
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no reachable Postgres for the run-trace tenant proof: {exc}")


async def _fresh(owner: asyncpg.Connection) -> None:
    """The migrations as a lane applies them, with the writer role they grant.

    The database is shared with the rest of the integration run, so nothing is
    dropped: the migrations are idempotent, this test's two tenants are added
    only if absent, and only their rows are cleared. Dropping the shared tenant
    mirror took every other test's seeded tenant with it.
    """
    for role in (_ROLE, _MIRROR_ROLE):
        exists = await owner.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role)
        if not exists:
            await owner.execute(f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOBYPASSRLS")
    for migration in _MIGRATIONS:
        await owner.execute(migration.read_text(encoding="utf-8"))
    # The writer role reads nothing on the mirror, as deployed: only the mirror's
    # own migration grants it, to the dashboard reader, which is what the
    # writer's mirror binding logs in as here. Nothing in this test grants or
    # revokes it, so the writer-role denial below is the migrations' own.
    await owner.executemany(
        "INSERT INTO public.tenant_registry_mirror (tenant_slug, tenant_uuid, status) "
        "VALUES ($1, $2, 'active') ON CONFLICT (tenant_slug) DO NOTHING",
        [(_SLUG_A, _UUID_A), (_SLUG_B, _UUID_B)],
    )
    await owner.execute(
        "DELETE FROM public.run_trace WHERE tenant_id = ANY($1::text[])", _OWN_TENANTS
    )


async def _own_rows(owner: asyncpg.Connection) -> int:
    return int(
        await owner.fetchval(
            "SELECT count(*) FROM public.run_trace WHERE tenant_id = ANY($1::text[])",
            _OWN_TENANTS,
        )
    )


class _Db:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool


def _writer(pool: asyncpg.Pool, mirror_pool: asyncpg.Pool) -> RunTraceProjectionWriter:
    db = _Db(pool)

    class _Writer(RunTraceProjectionWriter):
        def db_for(self, table: str, *, operation: str = "write") -> Any:
            if table == "tenant_registry_mirror" and operation == "read":
                return mirror_pool
            if table == "run_trace":
                return db
            raise AssertionError(f"undeclared relation {table!r} ({operation})")

        async def publish_snapshot_delta(self, exposure: Any, **kwargs: Any) -> bool:
            return True

    return _Writer()


async def _as_role_pool(role: str) -> asyncpg.Pool:
    async def _init(conn: asyncpg.Connection) -> None:
        await conn.execute(f"SET ROLE {role}")

    pool = await asyncpg.create_pool(_dsn_or_skip(), min_size=1, max_size=1, init=_init)
    assert pool is not None
    return pool


async def _read_as(owner: asyncpg.Connection, tenant: str) -> list[str]:
    async with owner.transaction():
        await owner.execute(f"SET LOCAL ROLE {_ROLE}")
        await owner.execute("SELECT set_config('app.tenant_id', $1, true)", tenant)
        rows = await owner.fetch("SELECT tenant_id FROM public.run_trace")
    return [str(r["tenant_id"]) for r in rows]


@pytest.mark.integration
async def test_real_postgres_a_slug_step_is_stored_under_the_mirror_uuid_and_read_by_it() -> (
    None
):
    owner = await _owner_or_skip()
    pool: asyncpg.Pool | None = None
    mirror_pool: asyncpg.Pool | None = None
    try:
        await _fresh(owner)
        pool = await _as_role_pool(_ROLE)
        mirror_pool = await _as_role_pool(_MIRROR_ROLE)
        row = await _writer(pool, mirror_pool)._project_step(
            {**_CHAIN[1], "tenant_id": _SLUG_A}, _META
        )
        assert row is not None
        assert row["tenant_id"] == _UUID_A
        assert await _read_as(owner, _UUID_A) == [_UUID_A]
        assert await _read_as(owner, _SLUG_A) == []
    finally:
        if mirror_pool is not None:
            await mirror_pool.close()
        if pool is not None:
            await pool.close()
        await owner.close()


@pytest.mark.integration
async def test_real_postgres_an_unknown_slug_writes_no_row() -> None:
    owner = await _owner_or_skip()
    pool: asyncpg.Pool | None = None
    mirror_pool: asyncpg.Pool | None = None
    try:
        await _fresh(owner)
        pool = await _as_role_pool(_ROLE)
        mirror_pool = await _as_role_pool(_MIRROR_ROLE)
        with pytest.raises(TenantRegistryResolutionError):
            await _writer(pool, mirror_pool)._project_step(
                {**_CHAIN[1], "tenant_id": "local-nobody-minted"}, _META
            )
        assert await _own_rows(owner) == 0
    finally:
        if mirror_pool is not None:
            await mirror_pool.close()
        if pool is not None:
            await pool.close()
        await owner.close()


@pytest.mark.integration
async def test_real_postgres_another_tenants_step_never_joins_the_run() -> None:
    owner = await _owner_or_skip()
    pool: asyncpg.Pool | None = None
    mirror_pool: asyncpg.Pool | None = None
    try:
        await _fresh(owner)
        pool = await _as_role_pool(_ROLE)
        mirror_pool = await _as_role_pool(_MIRROR_ROLE)
        writer = _writer(pool, mirror_pool)
        step = _CHAIN[1]
        await writer._project_step({**step, "tenant_id": _SLUG_A}, _META)
        await writer._project_step(
            {**step, "tenant_id": _SLUG_B, "reason": "other tenant"}, _META
        )
        assert await _read_as(owner, _UUID_A) == [_UUID_A]
        assert await _read_as(owner, _UUID_B) == [_UUID_B]
        # Positive control: both rows exist, so the reads above were narrowed.
        assert await _own_rows(owner) == 2
    finally:
        if mirror_pool is not None:
            await mirror_pool.close()
        if pool is not None:
            await pool.close()
        await owner.close()


@pytest.mark.integration
async def test_real_postgres_the_writer_role_cannot_read_the_mirror() -> None:
    """(F7) control: the slug resolutions above did not go through the writer role."""
    owner = await _owner_or_skip()
    try:
        await _fresh(owner)
        await owner.execute(f"SET ROLE {_ROLE}")
        try:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await owner.fetchval(
                    "SELECT count(*) FROM public.tenant_registry_mirror"
                )
        finally:
            await owner.execute("RESET ROLE")
        # Positive control: the role the mirror's own migration grants reads it.
        async with owner.transaction():
            await owner.execute(f"SET LOCAL ROLE {_MIRROR_ROLE}")
            assert (
                await owner.fetchval(
                    "SELECT count(*) FROM public.tenant_registry_mirror WHERE tenant_slug = $1",
                    _SLUG_A,
                )
                == 1
            )
    finally:
        await owner.close()
