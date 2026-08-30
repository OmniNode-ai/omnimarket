# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16804: real-Postgres proof that the registry-resolved tenant actually
lands in ``delegation_events.tenant_id``.

WHY THIS FILE EXISTS SEPARATELY FROM
``tests/test_omn16804_registry_resolved_write_tenant.py``

That module drives the same shipped handler, but against
``InmemoryDatabaseAdapter`` -- a dict of lists. It stores whatever Python object
it is handed, so a resolved tenant that is a ``str`` looks exactly as
"successful" as one Postgres would accept. The live column is ``uuid`` after
migration 0031/0032, and only a real connection enforces that. This is the same
gap that let the OMN-15905 str-where-``TIMESTAMPTZ`` defect reach a deployed,
CrashLoopBackOff-ing runtime with every mock-DB layer green, and it is why
``projection-write-path-db-gate`` refuses a write-path diff with no
real-Postgres companion. It refused this diff; this file is the answer, not an
annotation.

Real Postgres, never SQLite: SQLite would accept a ``str`` into a ``uuid``-ish
column without complaint, which is precisely the hole being closed.

The adapter below is deliberately a THIN, TEST-LOCAL sync shim over asyncpg
rather than a new shipped adapter. The write path under test
(``HandlerProjectionDelegation.project``) is synchronous and takes
``ProtocolProjectionDatabaseSync``; the repo ships only an async adapter, so a
test-local shim is the only way to drive the REAL sync handler against a REAL
database. It does no translation the handler could hide behind -- it binds the
row's values straight through, so a wrong Python type raises out of Postgres
exactly as it would in the runtime.

SKIPS (never ERRORs) without a reachable database, and provisions its own
throwaway schema so concurrent runs never collide -- the
``tests/test_writer_tenant_isolation_omn14898.py`` harness pattern.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from uuid import UUID, uuid4

import asyncpg
import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    TABLE,
    HandlerProjectionDelegation,
    ModelProjectionTaskDelegatedEvent,
)
from omnimarket.projection.tenant_isolation import _LEGACY_TENANT_UUID_MAP
from omnimarket.projection.tenant_registry_resolution import (
    TENANT_REGISTRY_MIRROR_TABLE,
    TenantRegistryResolutionError,
)

_MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_delegation"
    / "migrations"
)


# The FULL live migration set for this node, in the SAME sorted order the real
# runner applies it -- not a hand-picked base subset. Confirmed empirically, not
# assumed: driving project() against only 0007/0009a/0022 raises
# `asyncpg.exceptions.UndefinedColumnError: column "context_pack_hash" of
# relation "delegation_events" does not exist`, because the canonical write path
# writes columns added by LATER migrations (0016 quality-bar evidence, 0020
# context_pack_hash, ...). A gate that stops at the base tables passes for the
# wrong reason -- it never reaches most of the column set the live writer
# touches. This mirrors the same finding recorded in
# tests/test_omn15909_real_postgres_projection_write_path_gate.py.
#
# 0031 is the one deliberate exclusion: it is FENCED (OMN-15349) and superseded
# by omnibase_infra's 0032, and its own conversion carries a closed three-value
# literal map -- the exact thing OMN-16804 removes from the write path. This
# test performs the post-0032 conversion itself, below.
def _test_schema_safe_sql(raw_sql: str) -> str:
    """Strip ``CONCURRENTLY`` from ``CREATE INDEX`` for the test apply.

    ``CREATE INDEX CONCURRENTLY`` (migration 0029) refuses to run inside a
    transaction block, and asyncpg's simple-query ``execute()`` wraps a
    multi-statement string in an implicit transaction. ``CONCURRENTLY`` exists
    only to avoid locking a live table with real traffic, which is meaningless
    on a disposable single-connection schema, so a plain ``CREATE INDEX`` is
    schema-equivalent here. Same helper, same reasoning, as
    ``tests/test_omn15909_real_postgres_projection_write_path_gate.py``.
    """
    return raw_sql.replace("CREATE INDEX CONCURRENTLY", "CREATE INDEX")


_FENCED_MIGRATION = "0031_delegation_events_tenant_id_to_uuid.sql"


def _live_migration_files() -> list[Path]:
    return [
        path
        for path in sorted(_MIGRATIONS_DIR.glob("*.sql"))
        if path.name != _FENCED_MIGRATION
    ]


# The RLS migrations (0023/0026/0027) refuse to apply without the constrained
# read role -- deliberately, per OMN-14899: "RLS grants without the constrained
# read role are the exact bypass this work exists to prevent." Provisioned here
# exactly as tests/test_writer_tenant_isolation_omn14898.py does, NOLOGIN and
# scoped to the disposable schema, so no live credential is involved.
_APP_DASHBOARD_ROLE_SQL = """
DO $$
BEGIN
  BEGIN
    CREATE ROLE app_dashboard WITH
      NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
  EXCEPTION
    WHEN duplicate_object OR unique_violation THEN
      NULL;
  END;
END;
$$;
"""

_MIRROR_DDL = """
CREATE TABLE IF NOT EXISTS tenant_registry_mirror (
    tenant_slug         TEXT PRIMARY KEY,
    tenant_uuid         UUID NOT NULL,
    display_name        TEXT,
    status              TEXT NOT NULL,
    registry_created_at TIMESTAMPTZ,
    observed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_event_id     TEXT
);
"""

# The post-0032 shape. Without this the column stays TEXT and the assertion
# below would pass on a str -- a vacuous green, the exact class of proof this
# gate exists to reject. The policy from 0023 references the column, and
# PostgreSQL refuses ALTER COLUMN ... TYPE while a policy does, so it is dropped
# and recreated with the ::uuid cast -- byte-for-byte the sequence
# omnibase_infra's 0032 performs.
_CONVERT_TENANT_ID_TO_UUID = """
DROP POLICY IF EXISTS tenant_isolation ON delegation_events;
ALTER TABLE delegation_events ALTER COLUMN tenant_id DROP DEFAULT;
ALTER TABLE delegation_events
    ALTER COLUMN tenant_id TYPE UUID USING (NULLIF(tenant_id, '')::uuid);
CREATE POLICY tenant_isolation ON delegation_events
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
"""

FRESH_TENANT_SLUG = "beta-fresh-6a0c1d33"
FRESH_TENANT_UUID = UUID("6a0c1d33-1d0e-4a3f-9c2f-3b7d2f9a1c04")


def _dsn() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD not set -- skipping the OMN-16804 "
            "real-Postgres write-path proof"
        )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


class _SyncAsyncpgAdapter:
    """The narrowest sync ``ProtocolProjectionDatabaseSync`` over a real
    connection.

    One deliberate normalization, and exactly one: a ``str`` bound to a column
    PostgreSQL declares as a date/time type is parsed into a ``datetime`` first.
    This is not papering over the code under test -- it is the OMN-15905-class
    normalization any real sync adapter would have to perform, and the sync
    write path (``ProtocolProjectionDatabaseSync``) has NO shipped real-database
    implementation in this repo today, only ``InmemoryDatabaseAdapter``. Without
    it, every test here dies on `invalid input for query argument $3:
    '2026-...+00:00' (expected a datetime...)` before it ever reaches the
    identity assertion, which would make this file a proof about timestamps
    instead of about tenants. That sync-path timestamp representation is a real,
    PRE-EXISTING gap and is recorded as such on OMN-16804 -- it is not this
    ticket's change and is not fixed here.

    ``tenant_id`` is emphatically NOT normalized: it is bound exactly as the
    handler produced it, so a slug-shaped or ``str`` value fails against the
    ``uuid`` column instead of being quietly rescued. That is the whole point of
    the proof.
    """

    _TEMPORAL_TYPES = frozenset(
        {
            "timestamp with time zone",
            "timestamp without time zone",
            "date",
        }
    )

    def __init__(self, loop: asyncio.AbstractEventLoop, conn: asyncpg.Connection):
        self._loop = loop
        self._conn = conn
        self._temporal_columns: dict[str, frozenset[str]] = {}

    def _temporal_for(self, table: str) -> frozenset[str]:
        if table not in self._temporal_columns:
            records = self._loop.run_until_complete(
                self._conn.fetch(
                    "SELECT a.attname, format_type(a.atttypid, a.atttypmod) AS t "
                    "FROM pg_catalog.pg_attribute a "
                    "WHERE a.attrelid = $1::regclass "
                    "AND a.attnum > 0 AND NOT a.attisdropped",
                    table,
                )
            )
            self._temporal_columns[table] = frozenset(
                r["attname"] for r in records if r["t"] in self._TEMPORAL_TYPES
            )
        return self._temporal_columns[table]

    def _bind(self, table: str, column: str, value: Any) -> Any:
        if isinstance(value, str) and column in self._temporal_for(table):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value

    def upsert(self, table: str, conflict_key: str, row: dict[str, Any]) -> bool:
        columns = list(row)
        placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
        assignments = ", ".join(
            f"{c} = EXCLUDED.{c}" for c in columns if c != conflict_key
        )
        sql = (
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT ({conflict_key}) DO UPDATE SET {assignments}"
        )
        self._loop.run_until_complete(
            self._conn.execute(sql, *(self._bind(table, c, row[c]) for c in columns))
        )
        return True

    def query(
        self, table: str, filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        filters = filters or {}
        where = " AND ".join(f"{c} = ${i}" for i, c in enumerate(filters, start=1))
        sql = f"SELECT * FROM {table}" + (f" WHERE {where}" if where else "")
        records = self._loop.run_until_complete(
            self._conn.fetch(
                sql, *(self._bind(table, c, v) for c, v in filters.items())
            )
        )
        return [dict(r) for r in records]


def _event(correlation_id: str, tenant_slug: str) -> ModelProjectionTaskDelegatedEvent:
    return ModelProjectionTaskDelegatedEvent(
        correlation_id=correlation_id,
        tenant_id=tenant_slug,
        task_type="code-review",
        delegated_to="node_delegate_skill_orchestrator",
        model_name="qwen3.8",
    )


@pytest.fixture
def real_pg_adapter():
    """A migrated, disposable schema plus a sync adapter bound to it."""
    dsn = _dsn()
    loop = asyncio.new_event_loop()
    try:
        try:
            conn = loop.run_until_complete(asyncpg.connect(dsn))
        except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover
            pytest.skip(f"no reachable Postgres for the OMN-16804 proof: {exc}")
        schema = f"omn16804_{uuid4().hex[:12]}"
        try:
            loop.run_until_complete(conn.execute(f"CREATE SCHEMA {schema}"))
            loop.run_until_complete(
                conn.execute(f"SET search_path TO {schema}, public")
            )
            loop.run_until_complete(conn.execute(_APP_DASHBOARD_ROLE_SQL))
            for path in _live_migration_files():
                loop.run_until_complete(
                    conn.execute(
                        _test_schema_safe_sql(path.read_text(encoding="utf-8"))
                    )
                )
            loop.run_until_complete(conn.execute(_MIRROR_DDL))
            loop.run_until_complete(conn.execute(_CONVERT_TENANT_ID_TO_UUID))
            yield _SyncAsyncpgAdapter(loop, conn)
        finally:
            loop.run_until_complete(
                conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            )
            loop.run_until_complete(conn.close())
    finally:
        loop.close()


@pytest.mark.integration
def test_registry_resolved_tenant_lands_as_a_real_uuid(real_pg_adapter) -> None:
    """The GOAL row-5 probe against a real database.

    Materialize one registry row for a slug the compiled map has never heard of,
    run one delegation through the REAL handler, read the projection row back BY
    CORRELATION, and assert the stored ``tenant_id`` is the registry's UUID --
    as a ``uuid.UUID``, because the column is ``uuid`` and asyncpg returns what
    Postgres stored. Before this ticket the same event produced no row at all.
    """
    assert FRESH_TENANT_SLUG not in _LEGACY_TENANT_UUID_MAP

    real_pg_adapter.upsert(
        TENANT_REGISTRY_MIRROR_TABLE,
        "tenant_slug",
        {
            "tenant_slug": FRESH_TENANT_SLUG,
            "tenant_uuid": FRESH_TENANT_UUID,
            "status": "active",
            "source_event_id": str(uuid4()),
        },
    )

    correlation_id = str(uuid4())
    result = HandlerProjectionDelegation().project(
        _event(correlation_id, FRESH_TENANT_SLUG),
        real_pg_adapter,  # type: ignore[arg-type]
    )
    assert result.rows_upserted == 1

    rows = real_pg_adapter.query(TABLE, {"correlation_id": correlation_id})
    assert len(rows) == 1
    stored = rows[0]["tenant_id"]
    assert isinstance(stored, UUID), (
        "the column is `uuid` post-conversion; a str here would mean the write "
        f"path is still stamping a slug-shaped value. got {stored!r}"
    )
    assert stored == FRESH_TENANT_UUID


@pytest.mark.integration
def test_unprovisioned_tenant_writes_no_row_against_a_real_database(
    real_pg_adapter,
) -> None:
    """Fail-closed survives the real-DB path: an unattributable event never
    becomes a row, and it does not become a NULL-tenant row either.

    The mock-DB sibling asserts the raise. This asserts the consequence in the
    place that matters -- the table -- because a writer that raised AFTER a
    partial INSERT would satisfy the sibling and still leave a row behind.
    """
    correlation_id = str(uuid4())
    with pytest.raises(TenantRegistryResolutionError):
        HandlerProjectionDelegation().project(
            _event(correlation_id, "t-never-provisioned"),
            real_pg_adapter,  # type: ignore[arg-type]
        )
    assert real_pg_adapter.query(TABLE, {"correlation_id": correlation_id}) == []
