# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A cross-tenant read through the projection query node returns nothing, on real Postgres.

OMN-21288 (T17). The node app reads a tenant-scoped exposure from its table
through a role that is NOSUPERUSER and NOBYPASSRLS, on a throwaway PostgreSQL 16
container (never a live lane's database). The table is under FORCE ROW LEVEL
SECURITY with a policy on ``app.tenant_id``. Rows of two tenants are planted.

Failure modes each test is written against:

* (F1) the node serves tenant B's rows to a request for tenant A;
* (F2) a request that names no tenant is answered from the table instead of refused;
* (F3) the role the node reads as bypasses row-level security, so the proof of
  isolation rests on the query's WHERE clause alone -- asserted off the role's
  own catalogue row, and by a planted read that names the other tenant's rows
  and gets none;
* (F4) the isolation proof is vacuous because the planted rows are not there --
  the superuser reads both tenants' rows as the positive control;
* (F5) OMN-24458: a page held past its freshness is served stale without a
  table read in its request, and the refresh behind it reads the real table
  and holds the new rows, so the next read is a hit that carries them.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import asyncpg
import httpx
import pytest

from omnimarket.nodes.node_projection_query_api_effect.handlers.handler_projection_query import (
    HandlerProjectionQuery,
)
from omnimarket.nodes.node_projection_query_api_effect.ports.asgi_app import (
    create_app,
    get_row_source,
    get_topic_map,
)
from omnimarket.nodes.node_projection_query_api_effect.ports.contract_routes import (
    contract_page_cache,
)
from omnimarket.nodes.node_projection_query_api_effect.ports.page_cache import (
    ValkeyPageCache,
)
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.table_reader import TableRowSource

_DOCKER = shutil.which("docker")
if _DOCKER is None:
    pytest.skip(
        "docker unavailable -- cannot run the hermetic PostgreSQL 16 cross-tenant test",
        allow_module_level=True,
    )

_TENANT_A = "00000000-0000-4000-8000-0000000000a1"
_TENANT_B = "00000000-0000-4000-8000-0000000000b2"
_TENANT_NOBODY = "00000000-0000-4000-8000-0000000000c3"
_SCHEMA = "t17_projection"
_TABLE = "tenant_rows"
_TOPIC = "onex.snapshot.projection.t17-tenant-rows.v1"
_ROLE = "t17_node_reader"
_ROLE_SECRET = (
    "t17-throwaway-reader"  # a role of a container that is removed at teardown
)


@dataclass(frozen=True)
class _Postgres:
    host: str
    port: int
    database: str


async def _admin(server: _Postgres) -> asyncpg.Connection:
    return await asyncpg.connect(
        user="postgres",
        database=server.database,
        host=server.host,
        port=server.port,
        ssl=False,
    )


async def _accepts_sql(server: _Postgres) -> bool:
    try:
        connection = await _admin(server)
    except (OSError, asyncpg.PostgresError):
        return False
    try:
        return await connection.fetchval("SELECT 1") == 1
    finally:
        await connection.close()


@pytest.fixture(scope="module")
def postgres() -> Iterator[_Postgres]:
    """An owned localhost-only PostgreSQL 16 container, never a shared database."""
    name = f"t17-xtenant-pg-{uuid4().hex[:12]}"
    container_id: str | None = None
    try:
        created = subprocess.run(
            [
                str(_DOCKER),
                "run",
                "--detach",
                "--rm",
                "--name",
                name,
                "--label",
                "omnimarket.t17=cross-tenant-test",
                "--env",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                "--env",
                "POSTGRES_DB=t17",
                "--publish",
                "127.0.0.1::5432",
                "postgres:16-alpine",
            ],
            check=True,
            capture_output=True,
        )
        container_id = created.stdout.decode("utf-8").strip()
        published = (
            subprocess.run(
                [str(_DOCKER), "port", container_id, "5432/tcp"],
                check=True,
                capture_output=True,
            )
            .stdout.decode("utf-8")
            .strip()
            .splitlines()[0]
        )
        host, port = published.rsplit(":", maxsplit=1)
        assert host == "127.0.0.1"
        # The owned container is published through the INTEGRATION_POSTGRES_*
        # variables the other real-database tests read, so every connection
        # below is built from them and none can reach another database.
        with pytest.MonkeyPatch.context() as env:
            env.setenv("INTEGRATION_POSTGRES_HOST", host)
            env.setenv("INTEGRATION_POSTGRES_PORT", port)
            env.setenv("INTEGRATION_POSTGRES_DB", "t17")
            server = _Postgres(
                host=os.environ["INTEGRATION_POSTGRES_HOST"],
                port=int(os.environ["INTEGRATION_POSTGRES_PORT"]),
                database=os.environ["INTEGRATION_POSTGRES_DB"],
            )
            for _ in range(200):
                if asyncio.run(_accepts_sql(server)):
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("owned PostgreSQL 16 did not accept SELECT 1")
            yield server
    finally:
        if container_id is not None:
            subprocess.run(
                [str(_DOCKER), "rm", "--force", container_id],
                check=False,
                capture_output=True,
            )


_DDL = f"""
CREATE SCHEMA {_SCHEMA};
CREATE TABLE {_SCHEMA}.{_TABLE} (
    row_id text PRIMARY KEY,
    tenant_id text NOT NULL,
    created_at timestamptz NOT NULL
);
ALTER TABLE {_SCHEMA}.{_TABLE} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {_SCHEMA}.{_TABLE} FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON {_SCHEMA}.{_TABLE}
    USING (tenant_id = current_setting('app.tenant_id', true));
CREATE ROLE {_ROLE} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE
    NOREPLICATION PASSWORD '{_ROLE_SECRET}';
GRANT USAGE ON SCHEMA {_SCHEMA} TO {_ROLE};
GRANT SELECT ON {_SCHEMA}.{_TABLE} TO {_ROLE};
"""


@pytest.fixture
async def planted(postgres: _Postgres) -> AsyncIterator[asyncpg.Connection]:
    """The table under FORCE RLS with both tenants' rows planted; yields the superuser."""
    admin = await _admin(postgres)
    await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await admin.execute(f"DROP ROLE IF EXISTS {_ROLE}")
    await admin.execute(_DDL)
    await admin.executemany(
        f"INSERT INTO {_SCHEMA}.{_TABLE} (row_id, tenant_id, created_at) "
        "VALUES ($1, $2, now())",
        [
            ("a-1", _TENANT_A),
            ("a-2", _TENANT_A),
            ("b-1", _TENANT_B),
            ("b-2", _TENANT_B),
            ("b-3", _TENANT_B),
        ],
    )
    try:
        yield admin
    finally:
        await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await admin.execute(f"DROP ROLE IF EXISTS {_ROLE}")
        await admin.close()


def _exposure() -> ProjectionTableConfig:
    return ProjectionTableConfig(
        topic=_TOPIC,
        table=_TABLE,
        relation_schema=_SCHEMA,
        columns=("row_id", "tenant_id", "created_at"),
        order_by="created_at DESC",
        order_by_spec=(("created_at", "DESC", None),),
        freshness_column="created_at",
        source_contract="node_t17_fixture",
        bus_backed=True,
        key_columns=("row_id",),
        tenant_column="tenant_id",
    )


@pytest.fixture
async def client(
    postgres: _Postgres, planted: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    """The node app, reading as the NOBYPASSRLS role through the real table reader."""
    monkeypatch.delenv("ONEX_TENANT_ID", raising=False)
    dsn = (
        f"postgresql://{_ROLE}:{_ROLE_SECRET}"
        f"@{postgres.host}:{postgres.port}/{postgres.database}"
    )
    source = TableRowSource.for_database_url(dsn)
    app = create_app()
    topics = {_TOPIC: _exposure()}
    app.dependency_overrides[get_row_source] = lambda: source
    app.dependency_overrides[get_topic_map] = lambda: topics
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        await source.close()


async def _rows(client: httpx.AsyncClient, tenant: str | None) -> tuple[int, Any]:
    params = {} if tenant is None else {"tenant": tenant}
    response = await client.get(f"/projection-api/projection/{_TOPIC}", params=params)
    return response.status_code, response.json()


@pytest.mark.integration
async def test_the_role_is_nosuperuser_nobypassrls_and_the_table_forces_rls(
    planted: asyncpg.Connection,
) -> None:
    role = await planted.fetchrow(
        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = $1", _ROLE
    )
    relation = await planted.fetchrow(
        "SELECT c.relrowsecurity, c.relforcerowsecurity FROM pg_class AS c "
        "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
        "WHERE n.nspname = $1 AND c.relname = $2",
        _SCHEMA,
        _TABLE,
    )
    assert role is not None
    assert relation is not None
    assert (role["rolsuper"], role["rolbypassrls"]) == (False, False)
    assert (relation["relrowsecurity"], relation["relforcerowsecurity"]) == (True, True)


@pytest.mark.integration
async def test_the_superuser_sees_both_tenants_rows_as_the_positive_control(
    planted: asyncpg.Connection,
) -> None:
    counts = {
        row["tenant_id"]: row["n"]
        for row in await planted.fetch(
            f"SELECT tenant_id, count(*) AS n FROM {_SCHEMA}.{_TABLE} GROUP BY 1"
        )
    }
    assert counts == {_TENANT_A: 2, _TENANT_B: 3}


@pytest.mark.integration
async def test_a_request_for_a_tenant_returns_only_that_tenants_rows(
    client: httpx.AsyncClient,
) -> None:
    status_a, body_a = await _rows(client, _TENANT_A)
    status_b, body_b = await _rows(client, _TENANT_B)
    assert (status_a, status_b) == (200, 200)
    assert sorted(r["row_id"] for r in body_a["rows"]) == ["a-1", "a-2"]
    assert sorted(r["row_id"] for r in body_b["rows"]) == ["b-1", "b-2", "b-3"]
    assert {r["tenant_id"] for r in body_a["rows"]} == {_TENANT_A}


@pytest.mark.integration
async def test_a_tenant_with_no_rows_reads_nothing_of_the_others(
    client: httpx.AsyncClient,
) -> None:
    status, body = await _rows(client, _TENANT_NOBODY)
    assert status == 200
    assert body["rows"] == []
    assert body["row_count"] == 0


@pytest.mark.integration
async def test_a_request_naming_no_tenant_is_refused_before_any_read(
    client: httpx.AsyncClient,
) -> None:
    status, body = await _rows(client, None)
    assert status == 422
    assert body["error"] == "tenant_context_unresolved"
    assert "rows" not in body


@pytest.mark.integration
async def test_a_planted_read_of_the_other_tenants_rows_under_the_role_returns_nothing(
    postgres: _Postgres, planted: asyncpg.Connection
) -> None:
    """The query names tenant B's rows while the session carries tenant A.

    Row-level security, not the node's WHERE clause, refuses it: the statement
    below asks for B's rows explicitly.
    """
    connection = await asyncpg.connect(
        user=_ROLE,
        password=_ROLE_SECRET,
        database=postgres.database,
        host=postgres.host,
        port=postgres.port,
        ssl=False,
    )
    try:
        async with connection.transaction():
            await connection.execute(
                "SELECT set_config('app.tenant_id', $1, true)", _TENANT_A
            )
            planted_read = await connection.fetch(
                f"SELECT row_id FROM {_SCHEMA}.{_TABLE} WHERE tenant_id = $1",
                _TENANT_B,
            )
            own_read = await connection.fetch(
                f"SELECT row_id FROM {_SCHEMA}.{_TABLE} WHERE tenant_id = $1",
                _TENANT_A,
            )
    finally:
        await connection.close()
    assert planted_read == []
    assert sorted(r["row_id"] for r in own_read) == ["a-1", "a-2"]
    # The same statement as the superuser reads B's three rows: the rows exist.
    visible = await planted.fetch(
        f"SELECT row_id FROM {_SCHEMA}.{_TABLE} WHERE tenant_id = $1", _TENANT_B
    )
    assert len(visible) == 3


class _Clock:
    now = 0.0


class _Valkey:
    """The client calls the page cache makes, with a clock so a held page ages as its ``ex`` says."""

    def __init__(self, clock: _Clock) -> None:
        self._clock = clock
        self.held: dict[str, tuple[float, str]] = {}

    async def get(self, key: str) -> str | None:
        found = self.held.get(key)
        return None if found is None or found[0] <= self._clock.now else found[1]

    async def set(self, key: str, value: str, ex: int, nx: bool = False) -> bool | None:
        found = self.held.get(key)
        if nx and found is not None and found[0] > self._clock.now:
            return None
        self.held[key] = (self._clock.now + ex, value)
        return True

    async def delete(self, key: str) -> int:
        return 1 if self.held.pop(key, None) is not None else 0


class _CountingSource:
    """The real table reader, with a count of the table reads it is asked for."""

    def __init__(self, inner: TableRowSource) -> None:
        self._inner = inner
        self.reads = 0

    async def rows(self, *args: Any, **kwargs: Any) -> Any:
        self.reads += 1
        return await self._inner.rows(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@pytest.fixture
async def cached_client(
    postgres: _Postgres, planted: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[httpx.AsyncClient, _CountingSource, _Clock]]:
    """The node app with its page cache on a fake Valkey, reading the real table as the NOBYPASSRLS role."""
    monkeypatch.delenv("ONEX_TENANT_ID", raising=False)
    dsn = (
        f"postgresql://{_ROLE}:{_ROLE_SECRET}"
        f"@{postgres.host}:{postgres.port}/{postgres.database}"
    )
    real = TableRowSource.for_database_url(dsn)
    source = _CountingSource(real)
    clock = _Clock()
    app = create_app()
    topics = {_TOPIC: _exposure()}
    app.dependency_overrides[get_row_source] = lambda: source
    app.dependency_overrides[get_topic_map] = lambda: topics
    app.state.page_cache = ValkeyPageCache(
        contract_page_cache(), _Valkey(clock), clock=lambda: clock.now
    )
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c, source, clock
    finally:
        await real.close()


@pytest.mark.integration
async def test_a_stale_page_is_served_at_once_and_refreshed_from_the_real_table(
    cached_client: tuple[httpx.AsyncClient, _CountingSource, _Clock],
    planted: asyncpg.Connection,
) -> None:
    client, source, clock = cached_client
    _, first = await _rows(client, _TENANT_A)
    assert first["cache"]["state"] == "miss"
    assert sorted(r["row_id"] for r in first["rows"]) == ["a-1", "a-2"]
    assert source.reads == 1

    await planted.execute(
        f"INSERT INTO {_SCHEMA}.{_TABLE} (row_id, tenant_id, created_at) "
        "VALUES ('a-3', $1, now())",
        _TENANT_A,
    )
    clock.now = contract_page_cache().ttl_seconds + 5
    _, stale = await _rows(client, _TENANT_A)
    assert stale["cache"]["state"] == "stale"
    assert sorted(r["row_id"] for r in stale["rows"]) == ["a-1", "a-2"]

    await asyncio.gather(*HandlerProjectionQuery.pending_refreshes())
    assert source.reads == 2
    _, fresh = await _rows(client, _TENANT_A)
    assert fresh["cache"]["state"] == "hit"
    assert sorted(r["row_id"] for r in fresh["rows"]) == ["a-1", "a-2", "a-3"]
    assert source.reads == 2

    # the other tenant's page is its own page: no rows of A, and its own first read
    _, other = await _rows(client, _TENANT_B)
    assert other["cache"]["state"] == "miss"
    assert {r["tenant_id"] for r in other["rows"]} == {_TENANT_B}
