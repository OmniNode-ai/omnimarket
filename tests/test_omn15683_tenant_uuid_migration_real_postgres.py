# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15683: real-Postgres RED-before/GREEN-after proof for the
delegation_events tenant_id TEXT->UUID conversion, plus a cross-boundary seam
test binding the gateway's UUID tenant key to this table's projection write.

THE DEFECT (live-verified on onex-dev, 2026-08-03 and re-confirmed
2026-08-18): ``gateway_workflows.tenant_id`` (omninode_infra/onex-api,
database ``omninode_cloud``) is UUID. ``delegation_events.tenant_id`` (this
repo, database ``omnidash_analytics``) was TEXT holding the slug. The
dashboard read seam threads the gateway's UUID tenant claim into the
``app.tenant_id`` RLS GUC, so a UUID-keyed read of delegation_events under
FORCE RLS silently returned zero rows for every real provisioned tenant.

Structure:
  1. ``TestRedBeforeTheMigration`` -- reproduces the exact defect against the
     PRE-0031 schema (migrations 0000..0030 only): a row written under the
     slug key is invisible to a UUID-keyed read, visible to a slug-keyed
     read, over identical data. This is what the ticket's live probe found.
  2. ``TestGreenAfterTheMigration`` -- the SAME scenario against the FULL
     schema (migrations 0000..0031, the live production write path
     ``DelegationProjectionRunner``): a UUID-keyed read now returns the row,
     count-equal and primary-key-equal against an RLS-bypassing ground-truth
     query on the same UUID literal.
  3. ``TestCrossBoundarySeam`` -- OMN-14208 seam discipline (CLAUDE.md
     "define and match seams"): binds the GATEWAY side (an independently
     pinned UUID literal, matching the value ``omninode_cloud.public.
     tenants`` already carries for ``beta-business-proof`` per the ticket's
     live probe -- never derived from this repo's own tenant_isolation.py
     map) to the PROJECTION side (the real write path, driven with the real
     verified slug) via a real SQL JOIN against a companion table shaped
     like the real ``gateway_workflows``. A mutation control (reverting the
     writer to stamp the raw, unconverted slug -- the exact pre-fix shape)
     turns the join RED, proving this is not a vacuous two-independent-
     unit-suites check.

Harness pattern (``_connect_or_skip`` / disposable schema / guarded
``app_dashboard`` role / lexical migration glob) copied from
``tests/test_omn15909_real_postgres_projection_write_path_gate.py`` --
SKIPS (never ERRORs) without a reachable database, same env vars.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote, quote_plus
from uuid import UUID, uuid4

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta

pytestmark = pytest.mark.integration

_MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_delegation"
    / "migrations"
)

_TENANT_UUID_MIGRATION = "0031_delegation_events_tenant_id_to_uuid.sql"

# Independently pinned -- NOT imported from omnimarket.projection.tenant_isolation
# -- matching the OMN-15683 ticket's live probe against
# omninode_cloud.public.tenants on onex-dev (2026-08-03, re-confirmed
# 2026-08-18). If this repo's own mapping ever drifts from the gateway's real
# value, this test (not the implementation under test) is the independent
# check that catches it.
_BETA_BUSINESS_PROOF_SLUG = "beta-business-proof"
_BETA_BUSINESS_PROOF_UUID = UUID("91c74442-1233-4c97-b191-911a10346fdf")
_OTHER_TENANT_UUID = UUID("79afa726-3852-464f-b7a4-d4b8b9c75ee7")


def _live_migration_files(*, exclude: str | None = None) -> list[Path]:
    files = sorted(_MIGRATIONS_DIR.glob("*.sql"), key=lambda f: f.name)
    if exclude is not None:
        files = [f for f in files if f.name != exclude]
    return files


def _test_schema_safe_sql(raw_sql: str) -> str:
    """See test_omn15909's identical helper: strip CONCURRENTLY, which
    refuses to run inside asyncpg's implicit multi-statement transaction."""
    return raw_sql.replace("CREATE INDEX CONCURRENTLY", "CREATE INDEX")


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
ALTER ROLE app_dashboard
  NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
"""

# A genuinely non-superuser, NOBYPASSRLS LOGIN role for the READ assertions.
# ``admin_conn`` is a superuser -- it bypasses RLS unconditionally regardless
# of migration 0023/0031's FORCE ROW LEVEL SECURITY, so a tenant-scoped
# read/count run on it would return the true global count no matter what the
# ``app.tenant_id`` GUC says, making a RED-before/GREEN-after proof driven
# through it vacuous. This role reproduces the shape the real
# ``app_dashboard``-derived reader connects as (OMN-14899) so the RLS
# predicate genuinely gates the query.
_READER_ROLE = "omn15683_rls_reader"
_READER_PASSWORD = "omn15683-test-only-throwaway"

_READER_ROLE_SQL = f"""
DO $$
BEGIN
  BEGIN
    CREATE ROLE {_READER_ROLE} WITH
      LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION
      PASSWORD '{_READER_PASSWORD}';
  EXCEPTION
    WHEN duplicate_object THEN
      NULL;
  END;
END;
$$;
ALTER ROLE {_READER_ROLE}
  LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION
  PASSWORD '{_READER_PASSWORD}';
"""


def _reader_dsn(schema: str) -> str:
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    options = quote(f"-c search_path={schema},public")
    return (
        f"postgresql://{_READER_ROLE}:{_READER_PASSWORD}@{host}:{port}/{db}"
        f"?options={options}"
    )


# A minimal companion table shaped like the REAL gateway_workflows
# (omninode_infra, database omninode_cloud, a database this repo's test
# suite cannot reach): only the tenant_id column matters for the seam this
# test binds. Created in the SAME disposable schema as delegation_events so
# a single SQL JOIN can prove both sides key identically.
_GATEWAY_WORKFLOWS_FIXTURE_SQL = """
CREATE TABLE IF NOT EXISTS gateway_workflows_fixture (
    workflow_id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL
);
"""


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
            "POSTGRES_PASSWORD not set -- skipping OMN-15683 real-Postgres "
            "tenant-UUID migration gate"
        )
    try:
        return await asyncpg.connect(_base_dsn())
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for OMN-15683 migration gate: {exc}")


def _real_delegation_completed_payload(
    *, correlation_id: str, tenant_id: str
) -> dict[str, object]:
    return {
        "correlation_id": correlation_id,
        "tenant_id": tenant_id,
        "task_type": "code-review",
        "model_used": "glm-5.2",
        "content": "the model's real answer",
        "quality_passed": True,
        "quality_score": 0.95,
        "latency_ms": 1800,
        "prompt_tokens": 210,
        "completion_tokens": 480,
        "cumulative_attempt_cost": 0.0142,
        "cost_tier_name": "cheap_cloud",
        "context_pack_hash": "sha256:omn15683-real-db-gate",
    }


@asynccontextmanager
async def _provisioned_schema(
    *, include_tenant_uuid_migration: bool
) -> AsyncIterator[tuple[asyncpg.Connection, str]]:
    """Disposable schema migrated with either the pre-0031 or full migration
    set. Yields ``(admin_conn, schema)``; admin_conn is a superuser
    connection scoped via search_path, used for DDL, raw readback, and the
    gateway_workflows fixture table."""
    admin_conn = await _connect_or_skip()
    schema = f"omn15683_{uuid4().hex[:16]}"
    db_name = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    try:
        await admin_conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin_conn.execute(f"CREATE SCHEMA {schema}")
        await admin_conn.execute(f"SET search_path TO {schema}, public")
        await admin_conn.execute(_APP_DASHBOARD_ROLE_SQL)
        await admin_conn.execute(_READER_ROLE_SQL)
        await admin_conn.execute(
            f'GRANT CONNECT ON DATABASE "{db_name}" TO {_READER_ROLE}'
        )
        exclude = None if include_tenant_uuid_migration else _TENANT_UUID_MIGRATION
        for migration_path in _live_migration_files(exclude=exclude):
            sql = _test_schema_safe_sql(migration_path.read_text(encoding="utf-8"))
            await admin_conn.execute(sql)
        await admin_conn.execute(_GATEWAY_WORKFLOWS_FIXTURE_SQL)
        # The reader role needs USAGE + SELECT to run any RLS-covered query
        # at all -- migration 0023/0031 already GRANT SELECT to
        # app_dashboard specifically, but this throwaway role is not a
        # member of it, so grant directly.
        await admin_conn.execute(f"GRANT USAGE ON SCHEMA {schema} TO {_READER_ROLE}")
        await admin_conn.execute(
            f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {_READER_ROLE}"
        )
        yield admin_conn, schema
    finally:
        with contextlib.suppress(Exception):
            await admin_conn.execute("SET search_path TO public")
            await admin_conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin_conn.close()


async def _read_count_as_tenant(schema: str, tenant_guc_value: str) -> int:
    """Open a FRESH connection as the genuinely non-superuser, NOBYPASSRLS
    reader role, set the GUC, and count -- so the RLS predicate actually
    gates the result (see ``_READER_ROLE``'s docstring above)."""
    conn = await asyncpg.connect(_reader_dsn(schema))
    try:
        await conn.execute(
            "SELECT set_config('app.tenant_id', $1, false)", tenant_guc_value
        )
        return await conn.fetchval("SELECT count(*) FROM delegation_events")
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# 1. RED-before: reproduces the exact live defect against the pre-0031 schema.
# ---------------------------------------------------------------------------


class TestRedBeforeTheMigration:
    async def test_uuid_keyed_read_returns_zero_while_slug_keyed_read_returns_the_row(
        self,
    ) -> None:
        """Pins the exact OMN-15683 live finding as a committed regression:
        pre-conversion, delegation_events.tenant_id is TEXT holding the slug
        this repo's writer stamped -- the RLS predicate is a plain TEXT
        equality, so a UUID string never matches a slug string. The row is
        inserted directly (mirroring the pre-fix writer's exact behavior,
        since this branch's handler code no longer produces this shape) to
        isolate the schema-level defect from the writer fix proven in
        section 2/3 below.
        """
        async with _provisioned_schema(include_tenant_uuid_migration=False) as (
            admin_conn,
            schema,
        ):
            correlation_id = str(uuid4())
            await admin_conn.execute(
                "INSERT INTO delegation_events "
                "(correlation_id, tenant_id, task_type, delegated_to, timestamp) "
                "VALUES ($1, $2, $3, $4, now())",
                correlation_id,
                _BETA_BUSINESS_PROOF_SLUG,
                "code-review",
                "local-runtime",
            )

            uuid_keyed_count = await _read_count_as_tenant(
                schema, str(_BETA_BUSINESS_PROOF_UUID)
            )
            slug_keyed_count = await _read_count_as_tenant(
                schema, _BETA_BUSINESS_PROOF_SLUG
            )

            assert uuid_keyed_count == 0, (
                "RED-before: a UUID-keyed read must return ZERO rows against "
                "the pre-conversion TEXT column -- this is the live OMN-15683 "
                "defect (silent-wrong-answer, not an error)"
            )
            assert slug_keyed_count == 1, (
                "the same row, over identical data, must be visible under its "
                "slug key -- proves the row exists and the isolation gap is "
                "the KEY REPRESENTATION, not a missing row"
            )


# ---------------------------------------------------------------------------
# 2. GREEN-after: the full migration set, driven through the REAL live
#    production write path (DelegationProjectionRunner).
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _provisioned_runner_with_full_schema() -> AsyncIterator[
    tuple[DelegationProjectionRunner, asyncpg.Connection, str]
]:
    async with _provisioned_schema(include_tenant_uuid_migration=True) as (
        admin_conn,
        schema,
    ):
        pool = await asyncpg.create_pool(
            _base_dsn(),
            min_size=1,
            max_size=3,
            server_settings={"search_path": f"{schema},public"},
        )
        try:
            adapter = AsyncpgAdapter(dsn=_base_dsn())
            adapter._pool = pool  # type: ignore[attr-defined]
            runner = DelegationProjectionRunner()
            runner._db = adapter  # type: ignore[assignment]
            yield runner, admin_conn, schema
        finally:
            with contextlib.suppress(Exception):
                await pool.close()


class TestGreenAfterTheMigration:
    async def test_uuid_keyed_read_now_returns_the_row_count_and_pk_equal(
        self,
    ) -> None:
        """GREEN: the REAL production write path (DelegationProjectionRunner,
        the async Kafka-consuming runner), driven with the real verified
        slug ``event.tenant_id="beta-business-proof"`` -- the exact wire
        shape ``stamp_verified_tenant_slug`` produces -- against the FULL
        (post-0031) migrated schema. AC3: count-equal AND primary-key-equal
        against an RLS-bypassing ground-truth query on the same UUID
        literal, not merely non-empty.
        """
        async with _provisioned_runner_with_full_schema() as (
            runner,
            admin_conn,
            schema,
        ):
            correlation_id = str(uuid4())
            data = _real_delegation_completed_payload(
                correlation_id=correlation_id, tenant_id=_BETA_BUSINESS_PROOF_SLUG
            )
            meta = MessageMeta(partition=0, offset=0, fallback_id=correlation_id)

            ok = await runner.project_event(
                runner._topic_delegation_completed, data, meta
            )
            assert ok is True

            # The column value itself must be the canonical UUID, not the
            # raw slug string -- proves the writer boundary resolved it.
            # (admin_conn is a superuser here -- fine for this readback, it
            # is asserting the STORED value, not exercising RLS.)
            stored_tenant_id = await admin_conn.fetchval(
                "SELECT tenant_id FROM delegation_events WHERE correlation_id = $1",
                correlation_id,
            )
            assert stored_tenant_id == _BETA_BUSINESS_PROOF_UUID

            uuid_keyed_count = await _read_count_as_tenant(
                schema, str(_BETA_BUSINESS_PROOF_UUID)
            )
            assert uuid_keyed_count == 1, (
                "GREEN-after: a UUID-keyed read must now see the row -- the "
                "exact defect this migration closes"
            )

            # Ground truth: an RLS-BYPASSING (superuser admin_conn) query on
            # the same UUID literal, compared against the genuinely
            # RLS-SCOPED read (the non-superuser reader role, GUC set to the
            # same UUID) -- count-equal and primary-key-equal, not a
            # coincidental single-row match.
            ground_truth = await admin_conn.fetch(
                "SELECT correlation_id FROM delegation_events WHERE tenant_id = $1",
                _BETA_BUSINESS_PROOF_UUID,
            )
            reader_conn = await asyncpg.connect(_reader_dsn(schema))
            try:
                await reader_conn.execute(
                    "SELECT set_config('app.tenant_id', $1, false)",
                    str(_BETA_BUSINESS_PROOF_UUID),
                )
                rls_scoped = await reader_conn.fetch(
                    "SELECT correlation_id FROM delegation_events"
                )
            finally:
                await reader_conn.close()
            assert {r["correlation_id"] for r in ground_truth} == {
                r["correlation_id"] for r in rls_scoped
            }
            assert {r["correlation_id"] for r in ground_truth} == {correlation_id}

            # Isolation still holds: a different tenant's UUID sees nothing.
            other_tenant_count = await _read_count_as_tenant(
                schema, str(_OTHER_TENANT_UUID)
            )
            assert other_tenant_count == 0

    async def test_house_tenant_legacy_rows_backfill_to_the_house_tenant_uuid(
        self,
    ) -> None:
        """AC5: legacy rows are dispositioned explicitly, not silently. A row
        written under the pre-ruling default ('omninode', no explicit
        tenant) converts to HOUSE_TENANT_UUID by this migration's CASE
        expression -- proven here by inserting a pre-migration-shaped
        'omninode' row directly, then re-running JUST the conversion
        migration against it."""
        async with _provisioned_schema(include_tenant_uuid_migration=False) as (
            admin_conn,
            _schema,
        ):
            correlation_id = str(uuid4())
            await admin_conn.execute(
                "INSERT INTO delegation_events "
                "(correlation_id, tenant_id, task_type, delegated_to, timestamp) "
                "VALUES ($1, DEFAULT, $2, $3, now())",
                correlation_id,
                "code-review",
                "local-runtime",
            )
            migration_sql = _test_schema_safe_sql(
                (_MIGRATIONS_DIR / _TENANT_UUID_MIGRATION).read_text(encoding="utf-8")
            )
            await admin_conn.execute(migration_sql)

            tenant_id = await admin_conn.fetchval(
                "SELECT tenant_id FROM delegation_events WHERE correlation_id = $1",
                correlation_id,
            )
            assert tenant_id == UUID("820272f9-4aaf-5add-a2df-0af942852ab2")

    async def test_unmapped_legacy_value_aborts_the_whole_migration_transaction(
        self,
    ) -> None:
        """Fail-closed proof: a row under a tenant value OUTSIDE the closed
        mapping (simulating the OMN-15683 live-finding residual) must abort
        the migration with no partial conversion -- the column stays TEXT."""
        async with _provisioned_schema(include_tenant_uuid_migration=False) as (
            admin_conn,
            _schema,
        ):
            await admin_conn.execute(
                "INSERT INTO delegation_events "
                "(correlation_id, tenant_id, task_type, delegated_to, timestamp) "
                "VALUES ($1, $2, $3, $4, now())",
                str(uuid4()),
                "some-unreviewed-tenant-slug",
                "code-review",
                "local-runtime",
            )
            migration_sql = _test_schema_safe_sql(
                (_MIGRATIONS_DIR / _TENANT_UUID_MIGRATION).read_text(encoding="utf-8")
            )
            with pytest.raises(asyncpg.exceptions.RaiseError):
                await admin_conn.execute(migration_sql)

            column_type = await admin_conn.fetchval(
                "SELECT atttypid::regtype::text FROM pg_attribute "
                "WHERE attrelid = 'delegation_events'::regclass "
                "AND attname = 'tenant_id' AND NOT attisdropped"
            )
            assert column_type == "text", (
                "an aborted migration must leave the column TEXT -- no "
                "partial conversion"
            )


# ---------------------------------------------------------------------------
# 3. Cross-boundary seam: binds the (simulated) gateway UUID write to the
#    real projection write via a SQL JOIN. AC4.
# ---------------------------------------------------------------------------


class TestCrossBoundarySeam:
    async def test_gateway_uuid_and_projection_write_join_on_the_same_tenant(
        self,
    ) -> None:
        """GREEN: an independently pinned UUID (matching the ticket's live
        gateway_workflows probe, never imported from this repo's own
        tenant_isolation.py map) is inserted into a companion table shaped
        like the real gateway_workflows. The REAL projection write path is
        then driven with the REAL verified slug. A SQL JOIN between the two
        tables on tenant_id must find the pairing -- proving both sides
        resolve to the SAME identity, not merely that each individually
        looks plausible.
        """
        async with _provisioned_runner_with_full_schema() as (
            runner,
            admin_conn,
            _schema,
        ):
            gateway_workflow_id = str(uuid4())
            await admin_conn.execute(
                "INSERT INTO gateway_workflows_fixture (workflow_id, tenant_id) "
                "VALUES ($1, $2)",
                gateway_workflow_id,
                _BETA_BUSINESS_PROOF_UUID,
            )

            correlation_id = str(uuid4())
            data = _real_delegation_completed_payload(
                correlation_id=correlation_id, tenant_id=_BETA_BUSINESS_PROOF_SLUG
            )
            meta = MessageMeta(partition=0, offset=1, fallback_id=correlation_id)
            ok = await runner.project_event(
                runner._topic_delegation_completed, data, meta
            )
            assert ok is True

            joined = await admin_conn.fetch(
                "SELECT de.correlation_id, gw.workflow_id "
                "FROM delegation_events de "
                "JOIN gateway_workflows_fixture gw ON de.tenant_id = gw.tenant_id "
                "WHERE de.correlation_id = $1",
                correlation_id,
            )
            assert len(joined) == 1, (
                "the gateway's UUID tenant key and the projection write's "
                "resolved tenant key must join -- if they diverge, the "
                "dashboard's UUID-keyed read never sees this row (OMN-15683)"
            )
            assert str(joined[0]["workflow_id"]) == gateway_workflow_id

    async def test_mutation_control_reverting_the_writer_to_the_raw_slug_breaks_the_join(
        self,
    ) -> None:
        """RED (mutation control): patches the writer boundary back to the
        exact pre-fix shape (stamping the raw, unconverted slug into the
        UUID column) and proves the join then fails -- either because the
        write is rejected outright (invalid uuid literal) or, if it were
        somehow accepted, because the stored value would not equal the
        gateway's UUID. This is what makes section 3's GREEN test non-
        vacuous: it demonstrates the join actually depends on the fix, not
        on both sides happening to agree by construction.
        """
        async with _provisioned_runner_with_full_schema() as (
            runner,
            admin_conn,
            _schema,
        ):
            gateway_workflow_id = str(uuid4())
            await admin_conn.execute(
                "INSERT INTO gateway_workflows_fixture (workflow_id, tenant_id) "
                "VALUES ($1, $2)",
                gateway_workflow_id,
                _BETA_BUSINESS_PROOF_UUID,
            )

            correlation_id = str(uuid4())
            data = _real_delegation_completed_payload(
                correlation_id=correlation_id, tenant_id=_BETA_BUSINESS_PROOF_SLUG
            )
            meta = MessageMeta(partition=0, offset=2, fallback_id=correlation_id)

            with (
                patch(
                    "omnimarket.nodes.node_projection_delegation.handlers."
                    "handler_delegation.resolve_tenant_uuid_or_none",
                    side_effect=lambda value: value,  # pre-fix: raw slug through
                ),
                pytest.raises(asyncpg.exceptions.DataError),
            ):
                await runner.project_event(
                    runner._topic_delegation_completed, data, meta
                )

            rows = await admin_conn.fetch(
                "SELECT 1 FROM delegation_events WHERE correlation_id = $1",
                correlation_id,
            )
            assert rows == [], (
                "a rejected write (invalid uuid literal) must leave zero "
                "rows -- the mutation control proves the fix is load-bearing, "
                "not that Postgres happens to accept anything"
            )
