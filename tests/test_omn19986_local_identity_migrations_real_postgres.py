# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19986: real-Postgres proof of the local_identity migrations on their own.

The table's key, check and row-level security exist only in the migrations, so
only a real database proves them, and only a real database shows the Postgres
table and the local SQLite store carry the same columns. SKIPS without a
reachable database and uses its own throwaway schema and role, like the
local_environment migration proof.

Failure modes each test is written against:

* the migration does not apply, or does not apply twice;
* a constraint is declared twice, row-level security is not enabled or not
  forced, or a policy other than tenant_isolation decides what a session sees;
* the key is not the tenant, so one tenant can carry two identity rows;
* the database accepts a row the fold would never write (no tenant, an empty
  tenant, no mint time, an event id that is not a uuid);
* the Postgres table and the SQLite store drift apart;
* a row the fold plans does not satisfy the table's types;
* a session bound to one tenant reads or writes another tenant's identity, or a
  session with no tenant set reads or writes any row at all.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import asyncpg
import pytest

from omnimarket.models.model_local_identity_fold import plan_local_identity_write
from omnimarket.models.model_local_identity_minted import ModelLocalIdentityMinted
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

_ROOT = Path(__file__).resolve().parents[1]
_MIGRATIONS_DIR = (
    _ROOT / "src/omnimarket/nodes/node_projection_local_identity/migrations"
)
# The create and the FORCE step. 0001 grants a cluster role its privileges and
# is proven by omnibase_infra's live-apply test against a database with that role.
_MIGRATIONS = (
    _MIGRATIONS_DIR / "0000_create_local_identity.sql",
    _MIGRATIONS_DIR / "0002_force_rls_local_identity.sql",
)
_SCHEMA = "omn19986_local_identity_migrations_test"
_TENANT_A = "6c24401e-0000-4000-8000-00000000000a"
_TENANT_B = "11111111-2222-4333-8444-555555555555"
_AT = datetime(2026, 11, 2, 4, tzinfo=UTC)


def _dsn_or_skip() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD not set -- skipping local-identity proof"
        )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


async def _connect_or_skip() -> asyncpg.Connection:
    dsn = _dsn_or_skip()
    try:
        return await asyncpg.connect(dsn)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no reachable Postgres for local-identity migration proof: {exc}")


def _scoped(statement: str, schema: str) -> str:
    # The dashboard reader role is cluster-scoped and absent from a throwaway
    # database; the grant is kept, aimed at the connecting role.
    return statement.replace("public.", f"{schema}.").replace(
        "TO app_dashboard", "TO CURRENT_USER"
    )


async def _fresh(conn: asyncpg.Connection, schema: str) -> None:
    for migration in _MIGRATIONS:
        assert migration.is_file(), f"local_identity migration missing: {migration}"
    await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    await conn.execute(f"CREATE SCHEMA {schema}")
    # Twice: each migration is re-runnable over its own result.
    for _ in range(2):
        for migration in _MIGRATIONS:
            await conn.execute(_scoped(migration.read_text(encoding="utf-8"), schema))


def _row(tenant_id: str | None) -> dict[str, Any]:
    return {"tenant_id": tenant_id, "minted_at": _AT, "event_id": uuid.uuid4()}


async def _insert(conn: asyncpg.Connection, schema: str, row: dict[str, Any]) -> None:
    columns = ", ".join(row)
    values = ", ".join(f"${index}" for index in range(1, len(row) + 1))
    await conn.execute(
        f"INSERT INTO {schema}.local_identity ({columns}) VALUES ({values})",
        *row.values(),
    )


async def _drop_role(conn: asyncpg.Connection, role: str) -> None:
    exists = await conn.fetchval(
        "SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = $1", role
    )
    if exists:
        await conn.execute(f"DROP OWNED BY {role}")
        await conn.execute(f"DROP ROLE {role}")


async def _writer_role(conn: asyncpg.Connection, schema: str, role: str) -> None:
    """A role that is neither superuser nor owner, so the policy decides for it."""
    await _drop_role(conn, role)
    await conn.execute(f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOBYPASSRLS")
    await conn.execute(f"GRANT USAGE ON SCHEMA {schema} TO {role}")
    await conn.execute(
        f"GRANT SELECT, INSERT, UPDATE ON {schema}.local_identity TO {role}"
    )
    await conn.execute(f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA {schema} TO {role}")


@pytest.mark.integration
async def test_real_postgres_the_migration_declares_each_constraint_once() -> None:
    conn = await _connect_or_skip()
    schema = f"{_SCHEMA}_constraints"
    try:
        await _fresh(conn, schema)
        names = [
            r["conname"]
            for r in await conn.fetch(
                "SELECT conname FROM pg_constraint WHERE conrelid = $1::regclass "
                "ORDER BY conname",
                f"{schema}.local_identity",
            )
        ]
        assert names == ["local_identity_pkey", "local_identity_tenant_id_check"]
        key = await conn.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = $1::regclass AND contype = 'p'",
            f"{schema}.local_identity",
        )
        assert key == "PRIMARY KEY (tenant_id)"
        security = await conn.fetchrow(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE oid = $1::regclass",
            f"{schema}.local_identity",
        )
        assert security is not None
        assert (security["relrowsecurity"], security["relforcerowsecurity"]) == (
            True,
            True,
        )
        policies = await conn.fetch(
            "SELECT polname FROM pg_policy WHERE polrelid = $1::regclass",
            f"{schema}.local_identity",
        )
        assert [r["polname"] for r in policies] == ["tenant_isolation"]
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()


@pytest.mark.integration
async def test_real_postgres_the_table_and_the_local_store_carry_the_same_columns(
    tmp_path: Path,
) -> None:
    conn = await _connect_or_skip()
    schema = f"{_SCHEMA}_columns"
    try:
        await _fresh(conn, schema)
        postgres = {
            r["column_name"]
            for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = $1 AND table_name = 'local_identity'",
                schema,
            )
        }
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()
    store = tmp_path / "delegation.sqlite"
    # The store creates its tables when first used; a read is the first use.
    SqliteDatabaseAdapter(store).query("local_identity", {})
    with sqlite3.connect(store) as local:
        sqlite = {r[1] for r in local.execute("PRAGMA table_info(local_identity)")}
    assert postgres == {
        "tenant_id",
        "minted_at",
        "event_id",
        "updated_at",
        "projection_cursor",
    }
    assert postgres == sqlite


@pytest.mark.integration
async def test_real_postgres_one_tenant_has_one_identity_row() -> None:
    conn = await _connect_or_skip()
    schema = f"{_SCHEMA}_key"
    try:
        await _fresh(conn, schema)
        await _insert(conn, schema, _row(_TENANT_A))
        with pytest.raises(asyncpg.UniqueViolationError):
            await _insert(conn, schema, _row(_TENANT_A))
        # Positive control: another tenant is its own row.
        await _insert(conn, schema, _row(_TENANT_B))
        assert await conn.fetchval(f"SELECT count(*) FROM {schema}.local_identity") == 2
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    "override",
    [{"tenant_id": None}, {"tenant_id": ""}, {"minted_at": None}, {"event_id": None}],
)
async def test_real_postgres_the_table_refuses_what_the_fold_never_writes(
    override: dict[str, Any],
) -> None:
    conn = await _connect_or_skip()
    schema = f"{_SCHEMA}_checks"
    try:
        await _fresh(conn, schema)
        # Positive control: the unaltered row is accepted.
        await _insert(conn, schema, _row(_TENANT_A))
        await conn.execute(f"DELETE FROM {schema}.local_identity")
        with pytest.raises(
            (asyncpg.CheckViolationError, asyncpg.NotNullViolationError)
        ):
            await _insert(conn, schema, {**_row(_TENANT_A), **override})
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()


@pytest.mark.integration
async def test_real_postgres_the_row_the_fold_plans_satisfies_the_table() -> None:
    conn = await _connect_or_skip()
    schema = f"{_SCHEMA}_planned"
    try:
        await _fresh(conn, schema)
        event = ModelLocalIdentityMinted(tenant_id=uuid.UUID(_TENANT_A), minted_at=_AT)
        row = plan_local_identity_write(event, [], now=_AT)
        assert row is not None
        await conn.execute(
            f"INSERT INTO {schema}.local_identity "
            "(tenant_id, minted_at, event_id, updated_at) "
            "VALUES ($1, $2::text::timestamptz, $3::text::uuid, $4::text::timestamptz)",
            row["tenant_id"],
            row["minted_at"],
            row["event_id"],
            row["updated_at"],
        )
        stored = await conn.fetchrow(
            f"SELECT tenant_id, minted_at, event_id FROM {schema}.local_identity"
        )
        assert stored is not None
        assert (stored["tenant_id"], stored["minted_at"], stored["event_id"]) == (
            _TENANT_A,
            _AT,
            event.event_id,
        )
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()


@pytest.mark.integration
async def test_real_postgres_a_tenant_bound_session_neither_reads_nor_writes_another_tenants_row() -> (
    None
):
    conn = await _connect_or_skip()
    schema = f"{_SCHEMA}_tenant"
    role = "omn19986_local_identity_tenant_probe"
    try:
        await _fresh(conn, schema)
        await _writer_role(conn, schema, role)
        await _insert(conn, schema, _row(_TENANT_B))

        await conn.execute(f"SET ROLE {role}")
        await conn.fetchval("SELECT set_config('app.tenant_id', $1, false)", _TENANT_A)
        await _insert(conn, schema, _row(_TENANT_A))
        rewritten = await conn.execute(
            f"UPDATE {schema}.local_identity SET minted_at = NOW() "
            "WHERE tenant_id = $1",
            _TENANT_B,
        )
        assert rewritten == "UPDATE 0"
        seen = await conn.fetch(f"SELECT tenant_id FROM {schema}.local_identity")
        assert [r["tenant_id"] for r in seen] == [_TENANT_A]

        await conn.execute("SELECT set_config('app.tenant_id', '', false)")
        assert await conn.fetchval(f"SELECT count(*) FROM {schema}.local_identity") == 0
        with pytest.raises(
            asyncpg.InsufficientPrivilegeError, match="row-level security"
        ):
            await _insert(conn, schema, _row(_TENANT_B))

        await conn.execute("RESET ROLE")
        # Positive control: both rows are there, so the reads were narrowed.
        assert await conn.fetchval(f"SELECT count(*) FROM {schema}.local_identity") == 2
    finally:
        await conn.execute("RESET ROLE")
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await _drop_role(conn, role)
        await conn.close()
