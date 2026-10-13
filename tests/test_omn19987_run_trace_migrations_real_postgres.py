# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19987: real-Postgres proof of the run_trace migrations on their own.

The table's key, checks and row-level security exist only in the migrations, so
only a real database proves them. This file needs no node code: it applies the
SQL files and reads the database back. SKIPS without a reachable database and
uses its own throwaway schema and role, like the usage-by-model-day proof.

Failure modes each test is written against:

* the migration does not apply, or does not apply twice;
* a constraint is declared twice, row-level security is not enabled or not
  forced, or a policy other than tenant_isolation decides what a session sees;
* the database accepts a step the model would refuse (sequence 0, an unknown
  kind, a terminal naming itself as the failing step, no tenant);
* a session bound to one tenant reads, or writes, another tenant's step of the
  same run;
* a session with no tenant set reads or writes any step at all.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import asyncpg
import pytest

_ROOT = Path(__file__).resolve().parents[1]
_MIGRATIONS_DIR = _ROOT / "src/omnimarket/nodes/node_projection_run_trace/migrations"
# The create and the FORCE step. 0001 grants a cluster role its privileges and
# is proven on the lab against a database that has that role.
_MIGRATIONS = (
    _MIGRATIONS_DIR / "0000_create_run_trace.sql",
    _MIGRATIONS_DIR / "0002_force_rls_run_trace.sql",
)
_SCHEMA = "omn19987_run_trace_migrations_test"
_TENANT_A = "6c24401e-0000-4000-8000-00000000000a"
_TENANT_B = "11111111-2222-4333-8444-555555555555"
_RUN = "5b0b6c1e-3f52-4f0e-9a44-2f1f5d6f7a10"


def _dsn_or_skip() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip("INTEGRATION_POSTGRES_PASSWORD not set -- skipping run-trace proof")
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


async def _connect_or_skip() -> asyncpg.Connection:
    dsn = _dsn_or_skip()
    conn: asyncpg.Connection | None = None
    try:
        conn = await asyncpg.connect(dsn)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no reachable Postgres for run-trace migration proof: {exc}")
    assert conn is not None
    return conn


def _scoped(statement: str, schema: str) -> str:
    # The dashboard reader role is cluster-scoped and absent from a throwaway
    # database; the grant is kept, aimed at the connecting role.
    return statement.replace("public.", f"{schema}.").replace(
        "TO app_dashboard", "TO CURRENT_USER"
    )


async def _fresh(conn: asyncpg.Connection, schema: str) -> None:
    for migration in _MIGRATIONS:
        assert migration.is_file(), f"run_trace migration missing: {migration.name}"
    await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    await conn.execute(f"CREATE SCHEMA {schema}")
    # Twice: each migration is re-runnable over its own result.
    for _ in range(2):
        for migration in _MIGRATIONS:
            await conn.execute(_scoped(migration.read_text(encoding="utf-8"), schema))


def _step(tenant_id: str | None, step_sequence: int) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "correlation_id": _RUN,
        "step_sequence": step_sequence,
        "step_kind": "provider_call",
        "started_at": datetime(2026, 10, 11, 10, tzinfo=UTC),
        "duration_ms": 1,
        "outcome": "succeeded",
        "attempt_number": 1,
    }


async def _insert(conn: asyncpg.Connection, schema: str, row: dict[str, Any]) -> None:
    columns = ", ".join(row)
    values = ", ".join(f"${index}" for index in range(1, len(row) + 1))
    await conn.execute(
        f"INSERT INTO {schema}.run_trace ({columns}) VALUES ({values})", *row.values()
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
    await conn.execute(f"GRANT SELECT, INSERT ON {schema}.run_trace TO {role}")
    await conn.execute(f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA {schema} TO {role}")
    attributes = await conn.fetchrow(
        "SELECT rolsuper, rolbypassrls FROM pg_catalog.pg_roles WHERE rolname = $1",
        role,
    )
    assert attributes is not None
    assert (attributes["rolsuper"], attributes["rolbypassrls"]) == (False, False)


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
                f"{schema}.run_trace",
            )
        ]
        security = await conn.fetchrow(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE oid = $1::regclass",
            f"{schema}.run_trace",
        )
        assert security is not None
        assert (security["relrowsecurity"], security["relforcerowsecurity"]) == (
            True,
            True,
        )
        policies = await conn.fetch(
            "SELECT polname FROM pg_policy WHERE polrelid = $1::regclass",
            f"{schema}.run_trace",
        )
        assert [r["polname"] for r in policies] == ["tenant_isolation"]
        assert names == [
            "run_trace_attempt_number_check",
            "run_trace_duration_ms_check",
            "run_trace_failing_step_sequence_check",
            "run_trace_outcome_check",
            "run_trace_pkey",
            "run_trace_step_kind_check",
            "run_trace_step_sequence_check",
        ]
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    "override",
    [
        {"step_sequence": 0},
        {"step_kind": "thinking"},
        {"outcome": "maybe"},
        {"duration_ms": -1},
        {"attempt_number": 0},
        {"failing_step_sequence": 2},
        {"tenant_id": None},
        {"correlation_id": None},
    ],
)
async def test_real_postgres_the_table_refuses_what_the_model_refuses(
    override: dict[str, Any],
) -> None:
    """Written straight to the table, past the model: the constraints hold alone."""
    conn = await _connect_or_skip()
    schema = f"{_SCHEMA}_checks"
    try:
        await _fresh(conn, schema)
        row: dict[str, Any] = {
            "tenant_id": "t",
            "correlation_id": _RUN,
            "step_sequence": 2,
            "step_kind": "terminal",
            "started_at": datetime(2026, 10, 11, 10, tzinfo=UTC),
            "duration_ms": 1,
            "outcome": "failed",
            "attempt_number": 1,
            "failing_step_sequence": 1,
        }
        # Positive control: the unaltered row is accepted, so a refusal below is
        # the constraint under test and not a malformed statement.
        await _insert(conn, schema, row)
        await conn.execute(f"DELETE FROM {schema}.run_trace")
        with pytest.raises(
            (
                asyncpg.CheckViolationError,
                asyncpg.NotNullViolationError,
            )
        ):
            await _insert(conn, schema, {**row, **override})
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()


@pytest.mark.integration
async def test_real_postgres_a_tenant_bound_session_neither_reads_nor_writes_another_tenants_step() -> (
    None
):
    conn = await _connect_or_skip()
    schema = f"{_SCHEMA}_tenant"
    role = "omn19987_run_trace_tenant_probe"
    try:
        await _fresh(conn, schema)
        await _writer_role(conn, schema, role)
        # Another tenant's step of the same run, written by the owner.
        await _insert(conn, schema, _step(_TENANT_B, 1))

        await conn.execute(f"SET ROLE {role}")
        await conn.fetchval("SELECT set_config('app.tenant_id', $1, false)", _TENANT_A)
        await _insert(conn, schema, _step(_TENANT_A, 1))
        with pytest.raises(
            asyncpg.InsufficientPrivilegeError, match="row-level security"
        ):
            # Same run, a free step sequence: only the tenant differs.
            await _insert(conn, schema, _step(_TENANT_B, 2))
        seen = await conn.fetch(
            f"SELECT tenant_id, step_sequence FROM {schema}.run_trace"
        )
        assert [(r["tenant_id"], r["step_sequence"]) for r in seen] == [(_TENANT_A, 1)]

        await conn.execute("RESET ROLE")
        # Positive control: both tenants' steps are in the table, so the read
        # above was narrowed by the policy and not by an empty table.
        everything = await conn.fetch(
            f"SELECT tenant_id FROM {schema}.run_trace ORDER BY tenant_id"
        )
        assert [r["tenant_id"] for r in everything] == sorted([_TENANT_A, _TENANT_B])
    finally:
        await conn.execute("RESET ROLE")
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await _drop_role(conn, role)
        await conn.close()


@pytest.mark.integration
async def test_real_postgres_a_session_with_no_tenant_set_reads_and_writes_nothing() -> (
    None
):
    """The policy reads the tenant with missing_ok; an unset tenant must match no row."""
    conn = await _connect_or_skip()
    schema = f"{_SCHEMA}_unset"
    role = "omn19987_run_trace_unset_probe"
    try:
        await _fresh(conn, schema)
        await _writer_role(conn, schema, role)
        await _insert(conn, schema, _step(_TENANT_A, 1))
        await _insert(conn, schema, _step(_TENANT_B, 1))

        await conn.execute(f"SET ROLE {role}")
        # This connection never set app.tenant_id, so the setting is missing.
        assert (
            await conn.fetchval("SELECT current_setting('app.tenant_id', true)") is None
        )
        assert await conn.fetchval(f"SELECT count(*) FROM {schema}.run_trace") == 0
        with pytest.raises(
            asyncpg.InsufficientPrivilegeError, match="row-level security"
        ):
            await _insert(conn, schema, _step(_TENANT_A, 2))

        await conn.execute("RESET ROLE")
        assert await conn.fetchval(f"SELECT count(*) FROM {schema}.run_trace") == 2
    finally:
        await conn.execute("RESET ROLE")
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await _drop_role(conn, role)
        await conn.close()
