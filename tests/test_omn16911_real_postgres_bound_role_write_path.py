# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16911: real-Postgres proof that the bound DSN decides write-path access.

The unit coverage in ``tests/test_omn16911_consumer_flow_dsn_binding.py`` proves
the *plumbing* — that ``bind_projection_database_url`` reaches the adapter and
that the adapter refuses a rebind once a pool exists. It cannot prove the thing
that actually broke the ``.201`` dev lane, because no mock DB has an ACL: a
double accepts every statement regardless of which login role the DSN names.

Only real Postgres enforces ``USAGE`` on a schema. That is precisely the failure
this ticket exists to close — ``ConsumerFlowProjectionWriter`` dialled
``OMNIDASH_ANALYTICS_DB_URL`` (``role_omnidash``, no USAGE on
``omninode_internal``) while its SQL named ``omninode_internal``, so every
heartbeat raised ``InsufficientPrivilegeError: permission denied for schema
omninode_internal``, the DLQ climbed ~6/min and ``consumer_flow_windows`` held
0 rows through 482 denials. Every mock-DB layer of coverage passed the whole
time.

So this module provisions two throwaway login roles against a real server —
one granted the schema the way the topology grants ``omninode_runtime``, one
left bare the way ``role_omnidash`` is — and drives the SAME
``AsyncpgAdapter``/``BaseProjectionRunner`` seam the deployed writer uses:

  RED   — an adapter left on the unprivileged DSN is denied, with the live
          error class, against a real ACL.
  GREEN — after ``bind_projection_database_url`` hands it the granted DSN, the
          identical statement succeeds and a row lands.

SKIPS (never ERRORs) without a reachable Postgres, and provisions its own
uniquely-named schema and roles so concurrent runs never collide.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import quote_plus
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.projection.runner import BaseProjectionRunner


def _dsn_for(user: str, password: str) -> str:
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


def _base_dsn() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    return _dsn_for(user, password)


async def _connect_or_skip() -> asyncpg.Connection:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "POSTGRES_PASSWORD not set -- skipping OMN-16911 bound-role write-path gate"
        )
    try:
        return await asyncpg.connect(_base_dsn())
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for OMN-16911 bound-role gate: {exc}")


class _BoundRunner(BaseProjectionRunner):
    """Minimal concrete runner: the seam under test lives on the base class."""

    @property
    def topics(self) -> list[str]:  # pragma: no cover - shape only
        return []

    async def project_event(self, topic: str, data: dict, meta: object) -> bool:
        """Issue the write path's real shape: read the schema, then write it."""
        await self.db.execute(f"SELECT count(*) FROM {self._schema}.flow_windows")
        await self.db.execute(
            f"INSERT INTO {self._schema}.flow_windows (consumer_group) VALUES ($1)",
            data["consumer_group"],
        )
        return True


@asynccontextmanager
async def _granted_and_bare_roles() -> AsyncIterator[tuple[str, str, str]]:
    """Yield ``(schema, granted_dsn, bare_dsn)`` against a real server.

    ``granted`` mirrors the topology's ``omninode_runtime``: USAGE on the schema
    plus SELECT/INSERT on the relation, and NO superuser, NO RLS bypass.
    ``bare`` mirrors ``role_omnidash``: a valid login on the same database with
    no reach into this schema at all.
    """
    admin = await _connect_or_skip()
    suffix = uuid4().hex[:12]
    schema = f"omn16911_{suffix}"
    granted_role = f"omn16911_runtime_{suffix}"
    bare_role = f"omn16911_dash_{suffix}"
    password = f"pw_{suffix}"
    try:
        await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin.execute(f"CREATE SCHEMA {schema}")
        await admin.execute(
            f"CREATE TABLE {schema}.flow_windows ("
            "  id bigserial PRIMARY KEY,"
            "  consumer_group text NOT NULL)"
        )
        for role in (granted_role, bare_role):
            await admin.execute(
                f"CREATE ROLE {role} LOGIN NOSUPERUSER NOBYPASSRLS "
                f"PASSWORD '{password}'"
            )
        await admin.execute(f"GRANT USAGE ON SCHEMA {schema} TO {granted_role}")
        await admin.execute(
            f"GRANT SELECT, INSERT ON {schema}.flow_windows TO {granted_role}"
        )
        await admin.execute(
            f"GRANT USAGE, SELECT ON SEQUENCE {schema}.flow_windows_id_seq "
            f"TO {granted_role}"
        )
        yield (
            schema,
            _dsn_for(granted_role, password),
            _dsn_for(bare_role, password),
        )
    finally:
        with contextlib.suppress(asyncpg.PostgresError):
            await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        for role in (granted_role, bare_role):
            with contextlib.suppress(asyncpg.PostgresError):
                await admin.execute(f"DROP ROLE IF EXISTS {role}")
        await admin.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_unbound_dashboard_role_is_denied_by_a_real_acl() -> None:
    """RED: the live defect, reproduced against a real schema ACL.

    ``InsufficientPrivilegeError: permission denied for schema ...`` is the
    exact error class and message the .201 runtime logged 482 times. A mock DB
    cannot produce it, which is why every mock-backed test passed.
    """
    async with _granted_and_bare_roles() as (schema, _granted_dsn, bare_dsn):
        adapter = AsyncpgAdapter(dsn=bare_dsn, min_size=1, max_size=1)
        await adapter.connect()
        try:
            with pytest.raises(asyncpg.InsufficientPrivilegeError) as denied:
                await adapter.execute(f"SELECT count(*) FROM {schema}.flow_windows")
            assert "permission denied for schema" in str(denied.value)
        finally:
            await adapter.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_binding_the_topology_role_makes_the_write_path_land_a_row() -> None:
    """GREEN: the runtime's bind is what turns the denial into a written row.

    The runner is constructed on the bare DSN — the state the lane was in — and
    only the bind changes. AC2's ``count(*) > 0`` is asserted on the same
    connection the write went through.
    """
    async with _granted_and_bare_roles() as (schema, granted_dsn, bare_dsn):
        runner = _BoundRunner.__new__(_BoundRunner)
        runner._db = AsyncpgAdapter(dsn=bare_dsn, min_size=1, max_size=1)
        runner._schema = schema  # type: ignore[attr-defined]
        assert runner.db.dsn == bare_dsn

        runner.bind_projection_database_url(granted_dsn)
        assert runner.db.dsn == granted_dsn

        await runner.db.connect()
        try:
            assert await runner.project_event("", {"consumer_group": "g-1"}, None)
            rows = await runner.db.execute(
                f"SELECT count(*) AS n FROM {schema}.flow_windows"
            )
            assert rows[0]["n"] == 1
        finally:
            await runner.db.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_live_pool_cannot_have_its_role_swapped_underneath_it() -> None:
    """The rebind refusal, proved against a pool that is genuinely connected.

    The unit test asserts this with a sentinel object standing in for the pool.
    Here the pool is real and authenticated as the bare role, so the refusal is
    protecting a connection that actually exists.
    """
    async with _granted_and_bare_roles() as (_schema, granted_dsn, bare_dsn):
        adapter = AsyncpgAdapter(dsn=bare_dsn, min_size=1, max_size=1)
        await adapter.connect()
        try:
            assert adapter.is_connected is True
            with pytest.raises(RuntimeError, match="connected"):
                adapter.rebind(granted_dsn)
            assert adapter.dsn == bare_dsn
        finally:
            await adapter.close()
