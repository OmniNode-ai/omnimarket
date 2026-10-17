# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19988: real-Postgres proof of the local_environment migrations on their own.

The table's key, checks and row-level security exist only in the migrations, so
only a real database proves them, and only a real database shows the Postgres
table and the local SQLite store carry the same columns. SKIPS without a
reachable database and uses its own throwaway schema and role, like the
run-trace migration proof.

Failure modes each test is written against:

* the migration does not apply, or does not apply twice;
* a constraint is declared twice, row-level security is not enabled or not
  forced, or a policy other than tenant_isolation decides what a session sees;
* the database accepts a row the models would refuse (an unknown mode, state or
  verdict, an empty service, an alert without its severity or start, no tenant);
* the Postgres table and the SQLite store drift apart, so a field one serves the
  other does not;
* a session bound to one tenant reads, writes or rewrites another tenant's row;
* a session with no tenant set reads or writes any row at all.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import asyncpg
import pytest

from omnimarket.enums.enum_local_environment_mode import EnumLocalEnvironmentMode
from omnimarket.enums.enum_local_service_state import EnumLocalServiceState
from omnimarket.handlers.handler_local_environment_judgement import (
    load_local_alert_rules,
)
from omnimarket.models.model_local_environment_status import (
    ModelLocalEnvironmentStatus,
)
from omnimarket.nodes.node_projection_local_environment.handlers.handler_local_environment_store import (
    plan_local_environment_write,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

_ROOT = Path(__file__).resolve().parents[1]
_MIGRATIONS_DIR = (
    _ROOT / "src/omnimarket/nodes/node_projection_local_environment/migrations"
)
# The create and the FORCE step. 0001 grants a cluster role its privileges and
# is proven on the lab against a database that has that role.
_MIGRATIONS = (
    _MIGRATIONS_DIR / "0000_create_local_environment.sql",
    _MIGRATIONS_DIR / "0002_force_rls_local_environment.sql",
)
_SCHEMA = "omn19988_local_environment_migrations_test"
_TENANT_A = "6c24401e-0000-4000-8000-00000000000a"
_TENANT_B = "11111111-2222-4333-8444-555555555555"
_AT = datetime(2026, 10, 17, 4, tzinfo=UTC)


# The plan's rows carry times as ISO strings and versions as a dict; the
# Postgres columns are TIMESTAMPTZ and JSONB.
_TIME_COLUMNS = frozenset(
    {"last_seen", "last_change", "alert_since", "token_minted_at", "updated_at"}
)


async def _store_planned(
    conn: asyncpg.Connection, schema: str, row: dict[str, Any]
) -> None:
    columns = list(row)
    values = []
    for name in columns:
        if name == "versions":
            values.append(f"${len(values) + 1}::text::jsonb")
        elif name in _TIME_COLUMNS:
            values.append(f"${len(values) + 1}::text::timestamptz")
        else:
            values.append(f"${len(values) + 1}")
    updates = ", ".join(
        f"{c} = EXCLUDED.{c}"
        for c in columns
        if c not in {"tenant_id", "mode", "service"}
    )
    params = [json.dumps(row[c]) if c == "versions" else row[c] for c in columns]
    await conn.execute(
        f"INSERT INTO {schema}.local_environment ({', '.join(columns)}) "
        f"VALUES ({', '.join(values)}) "
        f"ON CONFLICT (tenant_id, mode, service) DO UPDATE SET {updates}",
        *params,
    )


def _dsn_or_skip() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD not set -- skipping local-environment proof"
        )
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
        pytest.skip(
            f"no reachable Postgres for local-environment migration proof: {exc}"
        )
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
        assert migration.is_file(), (
            f"local_environment migration missing: {migration.name}"
        )
    await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    await conn.execute(f"CREATE SCHEMA {schema}")
    # Twice: each migration is re-runnable over its own result.
    for _ in range(2):
        for migration in _MIGRATIONS:
            await conn.execute(_scoped(migration.read_text(encoding="utf-8"), schema))


def _row(tenant_id: str | None, service: str) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "mode": "compose",
        "service": service,
        "observed_state": "down",
        "reason": f"{service} exited",
        "last_seen": _AT,
        "last_change": _AT,
        "verdict": "down",
        "verdict_reason": f"{service} exited",
        "alert_code": "SERVICE_DOWN",
        "alert_severity": "critical",
        "alert_since": _AT,
        "report_id": f"report-{service}",
    }


async def _insert(conn: asyncpg.Connection, schema: str, row: dict[str, Any]) -> None:
    columns = ", ".join(row)
    values = ", ".join(f"${index}" for index in range(1, len(row) + 1))
    await conn.execute(
        f"INSERT INTO {schema}.local_environment ({columns}) VALUES ({values})",
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
        f"GRANT SELECT, INSERT, UPDATE ON {schema}.local_environment TO {role}"
    )
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
                f"{schema}.local_environment",
            )
        ]
        security = await conn.fetchrow(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE oid = $1::regclass",
            f"{schema}.local_environment",
        )
        assert security is not None
        assert (security["relrowsecurity"], security["relforcerowsecurity"]) == (
            True,
            True,
        )
        policies = await conn.fetch(
            "SELECT polname FROM pg_policy WHERE polrelid = $1::regclass",
            f"{schema}.local_environment",
        )
        assert [r["polname"] for r in policies] == ["tenant_isolation"]
        assert names == [
            "local_environment_alert_complete_check",
            "local_environment_alert_severity_check",
            "local_environment_mode_check",
            "local_environment_observed_state_check",
            "local_environment_pkey",
            "local_environment_service_check",
            "local_environment_verdict_check",
        ]
        key = await conn.fetchval(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = $1::regclass AND contype = 'p'",
            f"{schema}.local_environment",
        )
        assert key == "PRIMARY KEY (tenant_id, mode, service)"
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
                "WHERE table_schema = $1 AND table_name = 'local_environment'",
                schema,
            )
        }
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()
    store = tmp_path / "delegation.sqlite"
    # The store creates its tables when first used; a read is the first use.
    SqliteDatabaseAdapter(store).query("local_environment", {})
    with sqlite3.connect(store) as local:
        sqlite = {r[1] for r in local.execute("PRAGMA table_info(local_environment)")}
    assert len(postgres) == 21
    assert postgres == sqlite


@pytest.mark.integration
@pytest.mark.parametrize(
    "override",
    [
        {"mode": "kubernetes"},
        {"service": ""},
        {"observed_state": "starting"},
        {"verdict": "unknown"},
        {"alert_severity": "info"},
        {"alert_severity": None},
        {"alert_since": None},
        {"alert_code": None},
        {"tenant_id": None},
        {"report_id": None},
    ],
)
async def test_real_postgres_the_table_refuses_what_the_models_refuse(
    override: dict[str, Any],
) -> None:
    """Written straight to the table, past the models: the constraints hold alone."""
    conn = await _connect_or_skip()
    schema = f"{_SCHEMA}_checks"
    try:
        await _fresh(conn, schema)
        row = _row("t", "postgres")
        # Positive controls: the unaltered row, and a row with no alert at all,
        # are accepted, so a refusal below is the constraint under test and not
        # a malformed statement.
        await _insert(conn, schema, row)
        await _insert(
            conn,
            schema,
            {
                **_row("t", "valkey"),
                "verdict": "running",
                "alert_code": None,
                "alert_severity": None,
                "alert_since": None,
            },
        )
        await conn.execute(f"DELETE FROM {schema}.local_environment")
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
async def test_real_postgres_a_tenant_bound_session_neither_reads_nor_writes_another_tenants_row() -> (
    None
):
    conn = await _connect_or_skip()
    schema = f"{_SCHEMA}_tenant"
    role = "omn19988_local_environment_tenant_probe"
    try:
        await _fresh(conn, schema)
        await _writer_role(conn, schema, role)
        # Another tenant's row for the same service, written by the owner.
        await _insert(conn, schema, _row(_TENANT_B, "postgres"))

        await conn.execute(f"SET ROLE {role}")
        await conn.fetchval("SELECT set_config('app.tenant_id', $1, false)", _TENANT_A)
        await _insert(conn, schema, _row(_TENANT_A, "postgres"))
        with pytest.raises(
            asyncpg.InsufficientPrivilegeError, match="row-level security"
        ):
            await _insert(conn, schema, _row(_TENANT_B, "valkey"))
        # A rewrite aimed at the other tenant's row matches nothing.
        rewritten = await conn.execute(
            f"UPDATE {schema}.local_environment SET verdict = 'running' "
            "WHERE tenant_id = $1",
            _TENANT_B,
        )
        assert rewritten == "UPDATE 0"
        seen = await conn.fetch(
            f"SELECT tenant_id, service FROM {schema}.local_environment"
        )
        assert [(r["tenant_id"], r["service"]) for r in seen] == [
            (_TENANT_A, "postgres")
        ]

        await conn.execute("RESET ROLE")
        # Positive control: both tenants' rows are in the table, and B's is
        # unchanged, so the reads above were narrowed by the policy.
        everything = await conn.fetch(
            f"SELECT tenant_id, verdict FROM {schema}.local_environment "
            "ORDER BY tenant_id"
        )
        assert [(r["tenant_id"], r["verdict"]) for r in everything] == sorted(
            [(_TENANT_A, "down"), (_TENANT_B, "down")]
        )
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
    role = "omn19988_local_environment_unset_probe"
    try:
        await _fresh(conn, schema)
        await _writer_role(conn, schema, role)
        await _insert(conn, schema, _row(_TENANT_A, "postgres"))
        await _insert(conn, schema, _row(_TENANT_B, "postgres"))

        await conn.execute(f"SET ROLE {role}")
        assert (
            await conn.fetchval("SELECT current_setting('app.tenant_id', true)") is None
        )
        assert (
            await conn.fetchval(f"SELECT count(*) FROM {schema}.local_environment") == 0
        )
        with pytest.raises(
            asyncpg.InsufficientPrivilegeError, match="row-level security"
        ):
            await _insert(conn, schema, _row(_TENANT_A, "valkey"))

        await conn.execute("RESET ROLE")
        assert (
            await conn.fetchval(f"SELECT count(*) FROM {schema}.local_environment") == 2
        )
    finally:
        await conn.execute("RESET ROLE")
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await _drop_role(conn, role)
        await conn.close()


@pytest.mark.integration
async def test_real_postgres_every_row_the_plan_writes_satisfies_the_table() -> None:
    """Rows a SQLite store accepts must also satisfy the Postgres table's types and checks.

    SQLite takes any value in any column; Postgres refuses a time that is not a
    timestamp, versions that are not JSON, and an alert without its severity or
    start. Each row shape the plan produces -- the reported row, a re-judged
    sibling, a STALE row and a native row with its token mint time -- is
    written through the real table.
    """
    conn = await _connect_or_skip()
    schema = f"{_SCHEMA}_planned"
    rules = load_local_alert_rules()

    def report(
        service: str,
        state: EnumLocalServiceState,
        seconds: int,
        mode: EnumLocalEnvironmentMode = EnumLocalEnvironmentMode.COMPOSE,
        **extra: Any,
    ) -> ModelLocalEnvironmentStatus:
        return ModelLocalEnvironmentStatus(
            tenant_id=_TENANT_A,
            mode=mode,
            service=service,
            observed_state=state,
            reason=f"{service} {state.value}",
            observed_at=_AT + timedelta(seconds=seconds),
            versions={"omnimarket": "0.4.317"},
            **extra,
        )

    try:
        await _fresh(conn, schema)
        stored: list[dict[str, Any]] = []
        written: list[dict[str, Any]] = []
        for item in (
            report("postgres", EnumLocalServiceState.RUNNING, 0),
            report("valkey", EnumLocalServiceState.UNHEALTHY, 0),
            report("redpanda", EnumLocalServiceState.DOWN, 10),
            report("postgres", EnumLocalServiceState.RUNNING, 90),
            report(
                "local-runtime",
                EnumLocalServiceState.RUNNING,
                0,
                mode=EnumLocalEnvironmentMode.NATIVE,
                store_kind="sqlite",
                store_location="~/.omninode/delegation/delegation.sqlite",
                access_mode="loopback-token",
                token_minted_at=_AT,
                rotate_hint="restart onex dashboard",
            ),
        ):
            same_mode = [r for r in stored if r["mode"] == item.mode.value]
            rows = plan_local_environment_write(item, same_mode, rules, _AT)
            for row in rows:
                await _store_planned(conn, schema, row)
                stored = [
                    r
                    for r in stored
                    if (r["mode"], r["service"]) != (row["mode"], row["service"])
                ]
                stored.append(row)
            written.extend(rows)
        verdicts = {
            (r["mode"], r["service"]): (r["verdict"], r["alert_code"])
            for r in await conn.fetch(
                f"SELECT mode, service, verdict, alert_code FROM {schema}.local_environment"
            )
        }
        assert verdicts == {
            ("compose", "postgres"): ("running", None),
            ("compose", "valkey"): ("stale", "HEALTH_UNKNOWN"),
            ("compose", "redpanda"): ("stale", "HEALTH_UNKNOWN"),
            ("native", "local-runtime"): ("running", None),
        }
        # Positive control: the rows were not all one shape.
        assert {w["verdict"] for w in written} >= {
            "running",
            "unhealthy",
            "down",
            "stale",
        }
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()
